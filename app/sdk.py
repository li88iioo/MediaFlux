"""MediaFlux 的脚本接口：直接复用业务服务，不经过 Agent 工具协议。

函数返回可保存为 JSON 的完整业务结果。分页由调用脚本按返回的游标继续；
没有查到、来源失败和只取得部分数据保持原服务语义。
"""
from __future__ import annotations

import builtins
from dataclasses import asdict
from contextlib import contextmanager
from contextvars import ContextVar
from inspect import signature
from collections.abc import Callable, Mapping
from typing import Any, Literal, TypeVar
from types import EllipsisType, MappingProxyType


SDKMethodEffect = Literal["read", "write", "control", "local_write"]
_SDKFunction = TypeVar("_SDKFunction", bound=Callable[..., dict[str, Any]])


def sdk_method(effect: SDKMethodEffect):
    """声明业务读写或内部状态行为；不包装函数，签名与直接调用保持不变。"""
    if effect not in {"read", "write", "control", "local_write"}:
        raise ValueError(f"未知 SDK 方法类别：{effect}")

    def declare(function: _SDKFunction) -> _SDKFunction:
        setattr(function, "__sdk_effect__", effect)
        return function

    return declare


def _collect_methods(namespaces: Mapping[str, Any]):
    methods: dict[str, Callable[..., dict[str, Any]]] = {}
    effects: dict[str, SDKMethodEffect] = {}
    for namespace, instance in namespaces.items():
        for name, descriptor in vars(type(instance)).items():
            if name.startswith("_"):
                continue
            function = getattr(instance, name)
            effect = getattr(function, "__sdk_effect__", None)
            if not isinstance(descriptor, staticmethod) or effect not in {"read", "write", "control", "local_write"}:
                raise ValueError(f"SDK 方法 {namespace}.{name} 必须声明有效的行为类别")
            methods[f"{namespace}.{name}"] = function
            effects[f"{namespace}.{name}"] = effect
    return methods, MappingProxyType(effects)


_SESSION_CALL: ContextVar[Callable[[str, dict], dict] | None] = ContextVar("mediaflux_sdk_session_call", default=None)
_CALL_OWNER: ContextVar[str] = ContextVar("mediaflux_sdk_owner", default="")
_CALL_DELIVERY_ROUTE: ContextVar[Mapping[str, Any] | None] = ContextVar("mediaflux_sdk_delivery_route", default=None)
_PUBLICATION_CHECK: ContextVar[Callable | None] = ContextVar("mediaflux_sdk_publication_check", default=None)
_CALL_CANCELLED: ContextVar[Callable[[], bool] | None] = ContextVar("mediaflux_sdk_cancelled", default=None)
_READ_CHECKPOINT: ContextVar[Callable[[dict], None] | None] = ContextVar("mediaflux_sdk_read_checkpoint", default=None)
_RESOURCE_CANDIDATES: ContextVar[Mapping[str, Any] | None] = ContextVar("mediaflux_sdk_resource_candidates", default=None)


@contextmanager
def call_owner(owner: str, *, publication_check: Callable | None = None,
               cancelled: Callable[[], bool] | None = None, read_checkpoint: Callable[[dict], None] | None = None,
               delivery_route: Mapping[str, Any] | None = None, resource_candidates: Mapping[str, Any] | None = None,
               session_call: Callable[[str, dict], dict] | None = None):
    """服务端绑定已鉴权身份；不导出为工作区可调用方法或模型参数。"""
    session_token = _SESSION_CALL.set(session_call)
    token = _CALL_OWNER.set(owner)
    route_token = _CALL_DELIVERY_ROUTE.set(dict(delivery_route or {}))
    publication_token = _PUBLICATION_CHECK.set(publication_check)
    cancellation_token = _CALL_CANCELLED.set(cancelled)
    checkpoint_token = _READ_CHECKPOINT.set(read_checkpoint)
    candidates_token = _RESOURCE_CANDIDATES.set(resource_candidates)
    try:
        yield
    finally:
        _RESOURCE_CANDIDATES.reset(candidates_token)
        _CALL_DELIVERY_ROUTE.reset(route_token)
        _READ_CHECKPOINT.reset(checkpoint_token)
        _PUBLICATION_CHECK.reset(publication_token)
        _CALL_CANCELLED.reset(cancellation_token)
        _CALL_OWNER.reset(token)
        _SESSION_CALL.reset(session_token)


def _owner() -> str:
    owner = _CALL_OWNER.get()
    if not owner:
        raise ValueError("该方法需要服务端绑定调用身份")
    return owner


class Session:
    """宿主绑定的当前会话：已保存结果、未执行确认票据与可选的持久验收任务。"""
    @staticmethod
    @sdk_method('read')
    def result(handle: str, path: str = "") -> dict:
        """读取result_handle冻结快照的全文。path为JSON Pointer，默认空串读取根节点；data完整保留字典/列表/字符串/标量，不做模型窗口分页或截断，可保存JSON后自行切片。旧结果引用沿原TTL和owner/session校验，失效或路径错误会报错，不重新查询服务。TG引用保留untrusted_reference/source，不代表指令或写入授权。仅在宿主绑定的当前会话中可用。"""
        call = _SESSION_CALL.get()
        if call is None:
            raise ValueError("该方法需要宿主绑定当前会话")
        return call("result", {"handle": handle, "path": path})

    @staticmethod
    @sdk_method('control')
    def pending_plan(operation: str = "inspect") -> dict:
        """查看或取消当前会话未执行的确认卡，operation仅inspect/cancel。inspect保留原预览、冻结参数及TTL；cancel沿原票据取消保护，仅撤票，不执行或撤销业务。过期或重复取消返回pending=false；owner/session/lease由宿主绑定，不接受plan_id、身份或确认参数。修改业务参数应重新预检，确认仍由原确认入口处理。"""
        call = _SESSION_CALL.get()
        if call is None:
            raise ValueError("该方法需要宿主绑定当前会话")
        return call("pending_plan", {"operation": operation})

    @staticmethod
    @sdk_method('write')
    def batch(steps: list[dict]) -> dict:
        """将steps中的原生写method/arguments冻结为一张确认卡；参数与单方法相同。全部复核后串行执行，失败/unknown停止后项，不回滚、不重放。仅支持已声明可组合方法；后台方法只能是唯一第一项，后项须独立同步写，不能依赖前项的新结果。"""
        raise ValueError("组合写入需要原生工作区的确认执行器")

    @staticmethod
    @sdk_method('read')
    def task() -> dict:
        """读取当前会话持久验收任务、用户约束与已登记真实证据；没有任务返回empty，不要求普通查询建任务。思路、步骤、草稿可直接写/work里的笔记。"""
        call = _SESSION_CALL.get()
        if call is None:
            raise ValueError("该方法需要宿主绑定当前会话")
        return call("task", {})

    @staticmethod
    @sdk_method('control')
    def start_task(root_goal: str, constraints: list[str], obligations: list[dict], *, source: str) -> dict:
        """仅为需要跨回合恢复/严格证据验收的目标建持久任务；普通查询、计划笔记无需调用。source须为已接纳用户完整原文。obligations保留原验收合同：id/description/required=true/acceptance；配置检查用observed_component(component=tmdb|qbittorrent)，其它只读检查用observed_read(criterion=具体标准)。不授业务写权，不预猜工具名，不覆盖未完成任务；身份、lease、版本由宿主填充。"""
        call = _SESSION_CALL.get()
        if call is None:
            raise ValueError("该方法需要宿主绑定当前会话")
        return call("start_task", {"root_goal": root_goal, "constraints": constraints, "obligations": obligations, "source": source})

    @staticmethod
    @sdk_method('control')
    def revise_task(root_goal: str, constraints: list[str], obligations: list[dict], *, source: str) -> dict:
        """按当前用户原文修订已有持久验收任务；保留任务ID并推进epoch，旧证据失效，累计预算不重置。完整传入仍有效的用户约束；普通步骤改写用workspace笔记。"""
        call = _SESSION_CALL.get()
        if call is None:
            raise ValueError("该方法需要宿主绑定当前会话")
        return call("revise_task", {"root_goal": root_goal, "constraints": constraints, "obligations": obligations, "source": source})

    @staticmethod
    @sdk_method('control')
    def checkpoint_task(evidence_ids: list[str], blockers: list[str], *, read_evidence: dict[str, str] | None = None) -> dict:
        """仅在原持久验收场景关联任务中已登记的真实evidence_id和阻塞；不必每次业务RPC调用。配置验收传evidence_ids；其它只读验收用read_evidence={义务id:真实evidence_id}。证据须来自本任务、本epoch真实成功READ，不能用笔记勾选、result_handle或编造回执宣称完成。并行读取一次合并，完成仍由原reducer判定。"""
        call = _SESSION_CALL.get()
        if call is None:
            raise ValueError("该方法需要宿主绑定当前会话")
        return call("checkpoint_task", {"evidence_ids": evidence_ids, "blockers": blockers, "read_evidence": read_evidence})

    @staticmethod
    @sdk_method('control')
    def cancel_task(source: str) -> dict:
        """按当前用户原文取消持久任务跟踪；不取消待确认票据、不停止后台业务，也不撤销已执行操作。票据控制仍用pending_plan，业务停止沿各自原入口。"""
        call = _SESSION_CALL.get()
        if call is None:
            raise ValueError("该方法需要宿主绑定当前会话")
        return call("cancel_task", {"source": source})


class Subscriptions:
    """媒体追更订阅：真实订阅ID、完整分页、已播与库存核对、策略及全局通知配置。"""
    @staticmethod
    @sdk_method('read')
    def list(status: str = "", enabled: bool | None = None, limit: int = 200,
             before_updated_at: str | None = None, before_id: int | None = None) -> dict:
        """分页返回完整持久订阅及revision、已保存last_error/缺失/策略；按next续读，id是真实订阅编号。列表是持久快照，实时已播/库存差异用check_updates。"""
        from app.modules.media_subscriptions import get_media_subscription_service

        if (before_updated_at is None) != (before_id is None):
            raise ValueError("订阅续页必须同时提供时间和编号")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200 or (enabled is not None and not isinstance(enabled, bool)):
            raise ValueError("limit须为1到200整数，enabled须为布尔值或null")
        result = get_media_subscription_service().list_subscriptions(status=status, enabled=enabled, limit=limit,
            before=(before_updated_at, before_id) if before_updated_at is not None and before_id is not None else None)
        if result["next"] is not None:
            result["next"]["enabled"] = enabled
        return result

    @staticmethod
    @sdk_method('read')
    def get(subscription_id: int) -> dict:
        """读取一个真实订阅的完整配置、策略、revision、持久错误、缺失与工作流；不存在直接报错。"""
        from app.modules.media_subscriptions import get_media_subscription_service, strict_subscription_id

        return get_media_subscription_service().get_subscription(strict_subscription_id(subscription_id))

    @staticmethod
    @sdk_method('write')
    def create(provider: str, external_id: str, media_type: str, *, season: int | None = None,
               check_interval_minutes: int = 4320, action: str = "confirm", download_target: str = "guangya",
               sites: builtins.list[str] | None = None, enabled: bool = True) -> dict:
        """确认后为精确来源条目创建/恢复订阅；tmdb/douban/bangumi，非TMDB必须已有确认映射。season仅剧集1–100，间隔5–10080分钟。默认人工确认候选/光鸭；action=notify/confirm/auto，target=guangya/qb/both，可选sites/启停。auto确认后持续自动下载，整理/STRM只沿已有配置；不支持固定星期或独立4K字幕硬过滤。创建不立即搜索下载；返回真实ID。"""
        from app.modules.media_subscriptions import get_media_subscription_service

        service = get_media_subscription_service()
        args = {"provider": provider, "external_id": external_id, "media_type": media_type, "season": season, "check_interval_minutes": check_interval_minutes,
                "action": action, "download_target": download_target, "sites": sites, "enabled": enabled}
        prepared = service.prepare_change("create", args)
        return service.apply_change("create", prepared["arguments"], expected_snapshot=prepared["snapshot"])

    @staticmethod
    @sdk_method('write')
    def update(subscription_id: int, *, enabled: bool | EllipsisType = ..., monitor_mode: str | EllipsisType = ...,
               seasons: builtins.list[int] | EllipsisType = ..., include_specials: bool | EllipsisType = ...,
               action: str | EllipsisType = ..., download_target: str | EllipsisType = ...,
               check_interval_minutes: int | EllipsisType = ...) -> dict:
        """确认后更新指定订阅，未传字段保持原值；enabled控制暂停/恢复，策略含missing/future/selected、季度/特别篇、notify/confirm/auto、目标及周期。暂停/改策略使未提交工作失效，单独恢复启用保留在途工作；已进入提交阶段无法撤回，auto持续下载须确认。"""
        from app.modules.media_subscriptions import get_media_subscription_service

        args = {key: value for key, value in {"subscription_id": subscription_id, "enabled": enabled, "monitor_mode": monitor_mode,
            "seasons": seasons, "include_specials": include_specials, "action": action, "download_target": download_target,
            "check_interval_minutes": check_interval_minutes}.items() if value is not Ellipsis}
        service = get_media_subscription_service()
        prepared = service.prepare_change("update", args)
        return service.apply_change("update", prepared["arguments"], expected_snapshot=prepared["snapshot"])

    @staticmethod
    @sdk_method('write')
    def delete(subscription_id: int) -> dict:
        """确认后软删除真实订阅并停止检查，取消未提交候选/准入和运行中检查；保留已提交下载、媒体文件与历史。"""
        from app.modules.media_subscriptions import get_media_subscription_service

        service = get_media_subscription_service()
        prepared = service.prepare_change("delete", {"subscription_id": subscription_id})
        return service.apply_change("delete", prepared["arguments"], expected_snapshot=prepared["snapshot"])

    @staticmethod
    @sdk_method('read')
    def check_updates(subscription_id: int | None = None, limit: int = 8,
                      before_updated_at: str | None = None, before_id: int | None = None) -> dict:
        """只读实时比较TMDB已播与媒体库库存，完整返回本页每个订阅的缺失、future/unknown日期、库存完整性及资源搜索。每项最多搜索首个缺集，候选仍有界；不会触发下载或后台检查。省略ID分页全部订阅，按next读完；失败/暂停/不完整库存与无缺失不同。"""
        from app.modules.media_subscriptions import get_media_subscription_service

        return get_media_subscription_service().check_updates(subscription_id=subscription_id, limit=limit,
            before_updated_at=before_updated_at, before_id=before_id)

    @staticmethod
    @sdk_method('read')
    def notification(subscription_id: int) -> dict:
        """读取全局订阅通知开关、规则revision/订阅revision、explicit；default并不等于显式关闭。发送仍沿既有通知中心授权和目标，不是当前聊天私有配置。"""
        from app.modules.media_subscription_notifications import notification_rule

        return notification_rule(subscription_id)

    @staticmethod
    @sdk_method('write')
    def set_notification(subscription_id: int, *, enabled: bool | EllipsisType = ..., notify_on_missing: bool | EllipsisType = ...,
                         notify_on_satisfied: bool | EllipsisType = ..., notify_on_error: bool | EllipsisType = ...) -> dict:
        """确认后修改全局订阅通知开关，至少提供一项；未传字段保留。不会更改订阅策略或重定向聊天，返回explicit和实际版本。"""
        from app.modules.media_subscription_notifications import prepare_rule_change, apply_rule_change

        args = {key: value for key, value in {"subscription_id": subscription_id, "enabled": enabled,
            "notify_on_missing": notify_on_missing, "notify_on_satisfied": notify_on_satisfied, "notify_on_error": notify_on_error}.items() if value is not Ellipsis}
        prepared = prepare_rule_change("set_notification", args)
        return apply_rule_change("set_notification", prepared["arguments"], expected_snapshot=prepared["snapshot"])

    @staticmethod
    @sdk_method('write')
    def reset_notification(subscription_id: int) -> dict:
        """确认后删除显式通知规则，恢复默认（explicit=false、revision=0）；已经默认时不生成写计划。已入队通知不由此撤回，发送继续受原授权限制。"""
        from app.modules.media_subscription_notifications import prepare_rule_change, apply_rule_change

        prepared = prepare_rule_change("reset_notification", {"subscription_id": subscription_id})
        return apply_rule_change("reset_notification", prepared["arguments"], expected_snapshot=prepared["snapshot"])

    @staticmethod
    @sdk_method('read')
    def today() -> dict:
        """按本机明确日期窗口汇总管理员全局追更、整理、RSS、下载事件：计数是全量，content_titles为最多8个安全标题样本；保留timezone/as_of，不把请求失败当零动态。"""
        from app.modules.media_subscription_notifications import today_summary

        return today_summary()


class Automation:
    """每日主动摘要；沿原规则、调度和通知中心，不定时调用模型。媒体追更统一使用subscriptions。"""

    @staticmethod
    @sdk_method('read')
    def digests() -> dict:
        """完整读取当前身份全部每日摘要规则的真实rule_id、revision、启停、时间、设置及已保存的settings_error；默认未开启。其他身份不可见，不暴露通知目标。"""
        from app.modules.media_automation_rules import digests

        return digests(owner=_owner())

    @staticmethod
    @sdk_method('write')
    def set_digest(enabled: bool, *, rule_id: str = "", hour: int | EllipsisType = ...,
                   minute: int | EllipsisType = ..., errors_only: bool | EllipsisType = ...,
                   send_empty: bool | EllipsisType = ...) -> dict:
        """新增或修改每日摘要。新增必填hour；修改用digests读取的rule_id，未传设置保留，显式0/false生效。enabled=false停用；本机时区，复用既有通知开关/队列。通知目标由宿主身份和话题绑定，不能指定chat。"""
        from app.modules.media_automation_rules import prepare_set_digest, set_digest

        args = {key: value for key, value in {"enabled": enabled, "rule_id": rule_id, "hour": hour, "minute": minute,
                "errors_only": errors_only, "send_empty": send_empty}.items() if value is not Ellipsis}
        scope = {"owner": _owner(), "delivery_route": _CALL_DELIVERY_ROUTE.get()}
        return set_digest(args, **scope, expected_snapshot=prepare_set_digest(args, **scope)["snapshot"])


class Diagnostics:
    """Agent运行开关与全局只读诊断；数据库和日志仅对Web登录管理员开放。"""
    @staticmethod
    @sdk_method('read')
    def overview(probe: bool = False) -> dict:
        """读取完整工作区概览、来源事实和固定下一步；默认仅本地SQLite、配置、索引器声明，不探测网络。probe=True复用library.connection检查媒体节点；partial保留成功来源，未知连接计数保持null，空待办不证明服务健康。不扫描内容目录、不启动任务。"""
        from app.modules.workspace import overview

        if not _owner().startswith(("webk:v1:", "tg:v1:")):
            raise PermissionError("工作区诊断需要已授权的 Agent 身份")
        return overview(probe)

    @staticmethod
    @sdk_method('read')
    def search(query: str, sections: list[str] | None = None, limit: int = 20, offset: int = 0) -> dict:
        """按标题检索工作流；sections为library/rss/downloads/organize/local_media，默认全部。仅library访问媒体节点，其余仅读SQLite。保留真实id/request_id/file_id/source_id、媒体来源及open_url；不返回凭据或下载链接。limit1–50和offset按来源及媒体服务器独立分页，使用sections与has_more/next_offset继续，partial和未知分页不等于空或完成。同名不证明是同一任务链。"""
        from app.modules.workspace_search import search

        if not _owner().startswith(("webk:v1:", "tg:v1:")):
            raise PermissionError("工作区诊断需要已授权的 Agent 身份")
        return search(query, sections, limit, offset)

    @staticmethod
    @sdk_method('read')
    def configuration() -> dict:
        """完整读取媒体服务器、TMDB、qB、STRM、AI回退的配置事实、字段来源及全部错误；不返回配置键、原值或凭据，不探测连通性。配置不完整也是有效诊断。"""
        from app.modules.diagnostics import configuration

        if not _owner().startswith(("webk:v1:", "tg:v1:")):
            raise PermissionError("配置与链路诊断需要已授权的 Agent 身份")
        return configuration()

    @staticmethod
    @sdk_method('read')
    def component(component: str) -> dict:
        """读取一个配置组件的完整状态、字段来源、缺项、校验错误、能力影响和安全下一步。支持jellyfin/emby/tmdb/qbittorrent/strm/ai_recognition/discovery/douban/resource_results/indexer_search。不会修改配置或调用模型。"""
        from app.modules.diagnostics import component as read

        if not _owner().startswith(("webk:v1:", "tg:v1:")):
            raise PermissionError("配置与链路诊断需要已授权的 Agent 身份")
        return read(component)

    @staticmethod
    @sdk_method('read')
    def pipeline(limit: int = 20, offset: int = 0) -> dict:
        """完整保留本地自动化汇总与RSS公共诊断、来源及错误；limit1–100/offset分页RSS关注项，RSS has_more时可继续。部分来源失败保留其余事实，不以零计数冒充健康。不访问外网、不启动任务。"""
        from app.modules.diagnostics import pipeline

        if not _owner().startswith(("webk:v1:", "tg:v1:")):
            raise PermissionError("配置与链路诊断需要已授权的 Agent 身份")
        return pipeline(limit, offset)

    @staticmethod
    @sdk_method('read')
    def runtime() -> dict:
        """读取当前进程Agent、Telegram接入和模型路由开关；不探测服务连通性，不返回凭据或供应商配置。"""
        from app import config
        from app.agent.feature_gate import is_agent_enabled

        return {"agent_enabled": is_agent_enabled(),
                "telegram_enabled": config.get_bool("TG_AGENT_ENABLED", False),
                "model_routing_enabled": config.get_bool("AGENT_LLM_ENABLED")}

    @staticmethod
    @sdk_method('read')
    def query_db(sql: str = "", limit: int = 50) -> dict:
        """执行单条SELECT/WITH或省略sql查看可读表列。运维表与非凭据列白名单、数据库只读连接；limit 1–200，truncated表示还有行，可改SQL分页/聚合。长字段完整返回并脱敏，数据库快照不代表上游实时状态。"""
        from app.modules.diagnostics import query_db

        if not _owner().startswith("webk:v1:"):
            raise PermissionError("全局诊断仅对 Web 登录身份开放")
        return query_db(sql, limit)

    @staticmethod
    @sdk_method('read')
    def search_logs(query: str = "", level: str = "", date: str = "", limit: int = 50) -> dict:
        """查当天或YYYY-MM-DD的最近匹配日志；level为最低级别。返回完整脱敏堆栈、matched_in_scan/scan_complete/truncated；最多扫描末尾32MiB，未命中不保证全文件没有记录。仅Web登录身份。"""
        from app.modules.diagnostics import search_logs

        if not _owner().startswith("webk:v1:"):
            raise PermissionError("全局诊断仅对 Web 登录身份开放")
        return search_logs(query, level, date, limit)


class Skills:
    """当前用户的长期技能库：保存可复用方法、查看修订和恢复历史版本。"""
    @staticmethod
    @sdk_method('read')
    def list(name: str | None = None) -> dict:
        """列出已保存技能的名称、简介和revision，不返回正文。指定name读取该技能修订列表。内置流程在/skills。"""
        from app.repositories.agent_skills import list_skills

        return {"items": list_skills(owner=_owner(), name=name)}

    @staticmethod
    @sdk_method('read')
    def get(name: str, revision: int | None = None) -> dict:
        """读取完整SKILL.md和元信息；默认最新版，不存在时item为null。"""
        from app.repositories.agent_skills import get_skill

        return {"item": get_skill(owner=_owner(), name=name, revision=revision)}

    @staticmethod
    @sdk_method('local_write')
    def save(name: str, description: str, content: str, expected_revision: int = 0) -> dict:
        """保存已验证的方法，跨会话保留。name用小写字母/数字/连字符/下划线；新建revision=0，更新传读取的revision防覆盖并发修改。不要保存凭据、临时库存或未经核实的结论。"""
        from app.repositories.agent_skills import save_skill

        return {"item": save_skill(owner=_owner(), name=name, description=description, content=content,
                                   expected_revision=expected_revision, publication_check=_PUBLICATION_CHECK.get())}

    @staticmethod
    @sdk_method('local_write')
    def restore(name: str, revision: int, expected_revision: int) -> dict:
        """将指定历史版本恢复为一个新修订；expected_revision是当前版本，已有历史不删除。"""
        from app.repositories.agent_skills import restore_skill

        return {"item": restore_skill(owner=_owner(), name=name, revision=revision,
                                      expected_revision=expected_revision, publication_check=_PUBLICATION_CHECK.get())}


class Preferences:
    """当前用户显式保存的媒体偏好与长期备忘；工作区变更须确认，不从对话自行推断。"""
    @staticmethod
    @sdk_method('read')
    def get() -> dict:
        """读取当前身份的媒体偏好、memory_notes 与 explicit 标记；不包含其他用户或私有版本 token。"""
        from app.database import media_owner_digest
        from app.modules.media_preferences import public_media_preferences
        from app.repositories.media_experience import get_media_preferences

        return public_media_preferences(get_media_preferences(media_owner_digest(_owner())))

    @staticmethod
    @sdk_method('write')
    def update(*, preferred_server: str | EllipsisType = ..., preferred_download_target: str | EllipsisType = ...,
               preferred_resolution: str | EllipsisType = ..., minimum_resolution: str | EllipsisType = ..., preferred_hdr: str | EllipsisType = ...,
               preferred_codecs: list[str] | EllipsisType = ..., preferred_subtitles: list[str] | EllipsisType = ...,
               preferred_audio_languages: list[str] | EllipsisType = ..., preferred_release_groups: list[str] | EllipsisType = ...,
               excluded_keywords: list[str] | EllipsisType = ..., preferred_genres: list[str] | EllipsisType = ..., excluded_genres: list[str] | EllipsisType = ...,
               max_episode_size_gb: float | EllipsisType = ..., min_rating: float | EllipsisType = ..., exclude_played: bool | EllipsisType = ...,
               memory_notes: list[str] | EllipsisType = ...) -> dict:
        """按字段修改显式偏好，如update(preferred_resolution="2160p")，工作区先预览确认。优先4K只设preferred_resolution；仅“至少/不得低于”设minimum_resolution。memory_notes用完整新列表；空数组/0/False是有效修改，Ellipsis表示未提供，未传字段保留。"""
        changes = {key: value for key, value in locals().items() if value is not Ellipsis}
        from app.database import media_owner_digest
        from app.modules.media_preferences import apply_media_preferences_change, prepare_media_preferences_change, public_media_preferences, validate_preference_updates

        owner = media_owner_digest(_owner())
        prepared = prepare_media_preferences_change(owner, validate_preference_updates(changes))
        updated = apply_media_preferences_change(owner, prepared["updates"], expected_fingerprint=prepared["fingerprint"])
        return {"ok": updated is not None, "status": "completed" if updated is not None else "conflict",
                "preferences": public_media_preferences(updated) if updated is not None else None}

    @staticmethod
    @sdk_method('write')
    def clear() -> dict:
        """确认后清除当前用户全部显式偏好和长期备忘，恢复产品默认；不改变系统配置、媒体或下载任务。"""
        from app.database import media_owner_digest
        from app.modules.media_preferences import apply_media_preferences_change, prepare_media_preferences_change

        owner = media_owner_digest(_owner())
        prepared = prepare_media_preferences_change(owner)
        if not prepared["current"]["explicit"]:
            return {"ok": False, "status": "precondition_failed"}
        cleared = apply_media_preferences_change(owner, expected_fingerprint=prepared["fingerprint"])
        return {"ok": cleared is not None, "status": "completed" if cleared is not None else "conflict"}


class RSS:
    """RSS规则订阅的配置与本地状态；条目下载、刷新是独立操作。"""
    @staticmethod
    @sdk_method('read')
    def subscriptions(limit: int = 100, after_id: int | None = None) -> dict:
        """完整配置与调度/条目计数；URLs中凭据脱敏。has_more时用next续页。"""
        from app.modules.rss_subscriptions import subscriptions
        return subscriptions(limit, after_id)

    @staticmethod
    @sdk_method('read')
    def subscription(subscription_id: int) -> dict:
        """按真实subscription_id读取完整配置和聚合状态。"""
        from app.modules.rss_subscriptions import subscription
        return subscription(subscription_id)

    @staticmethod
    @sdk_method('read')
    def activity(limit: int = 100, after_id: int | None = None, run_id: int | None = None) -> dict:
        """最近24小时成功下载总数和逐订阅计数；next续页。run_id读取对应刷新持久回执，不回退最近任务。"""
        if run_id is not None:
            from app.modules.rss_delivery import refresh_run
            return refresh_run(run_id)
        from app.modules.rss_subscriptions import activity
        return activity(limit, after_id)

    @staticmethod
    @sdk_method('read')
    def diagnose(limit: int = 20, offset: int = 0) -> dict:
        """只读本地RSS调度、积压、失败和不一致状态，不访问订阅源；attention.next可继续读取关注项。"""
        from app.modules.rss_subscriptions import diagnose
        return diagnose(limit, offset)

    @staticmethod
    @sdk_method('read')
    def entries(subscription_id: int | None = None, status: str = "pending", limit: int = 100, offset: int = 0, query: str = "") -> dict:
        """按真实条目 ID 分页读取本地条目与持久 delivery；next 续页，标题不截断，不返回 payload/凭据。"""
        from app.modules.rss_delivery import entries
        return entries(subscription_id, status, limit, offset, query)

    @staticmethod
    @sdk_method('write')
    def mark(entry_ids: list[int], processed: bool) -> dict:
        """确认后原子标记完整 ID 集合；不会覆盖正在提交、已下载或未知投递状态。"""
        from app.modules.rss_delivery import execute
        return execute("mark", {"entry_ids": entry_ids, "processed": processed})

    @staticmethod
    @sdk_method('write')
    def refresh(subscription_ids: list[int] | None = None, scope: str | None = None) -> dict:
        """确认后刷新指定 ID 或 scope=all_configured（含停用项），不自动下载；run_id 可用 activity(run_id=...) 读取持久回执。"""
        from app.modules.rss_delivery import execute
        return execute("refresh", {"subscription_ids": subscription_ids, "scope": scope})

    @staticmethod
    @sdk_method('write')
    def download(entry_ids: list[int] | None = None, *, limit: int | None = None, subscription_id: int | None = None) -> dict:
        """确认后投递指定 pending ID 集合；省略 entry_ids 则取最近 limit 项（默认10），可按订阅过滤，has_more 明示剩余。目标沿订阅 qB/光鸭，受理不等于下载完成；entries 的 delivery 可跟踪。"""
        from app.modules.rss_delivery import execute
        return execute("download", {"entry_ids": entry_ids, "limit": limit, "subscription_id": subscription_id})

    @staticmethod
    @sdk_method('write')
    def retry(entry_ids: list[int] | None = None, *, limit: int | None = None, subscription_id: int | None = None) -> dict:
        """确认后仅重试满足原冷却/次数/失败分类的条目；范围参数同 download，正在提交或未知结果不重放。"""
        from app.modules.rss_delivery import execute
        return execute("retry", {"entry_ids": entry_ids, "limit": limit, "subscription_id": subscription_id})

    @staticmethod
    @sdk_method('write')
    def create(*, name: str | EllipsisType = ..., urls: str | list[str] | EllipsisType = ..., exclude_keywords: str | EllipsisType = ..., action: str | EllipsisType = ..., enabled: bool | EllipsisType = ..., refresh_interval_minutes: int | EllipsisType = ..., download_method: str | EllipsisType = ..., media_tmdb_id: str | EllipsisType = ..., media_default_season: int | EllipsisType = ..., skip_existing_episodes: bool | EllipsisType = ..., parser: str | EllipsisType = ..., refresh_cron: str | EllipsisType = ..., qb_save_path: str | EllipsisType = ..., gy_target_dir: str | EllipsisType = ..., gy_target_dir_name: str | EllipsisType = ...) -> dict:
        """创建RSS配置，name和urls必填；沿Web规则校验全部字段，不立即刷新或下载。工作区需确认。"""
        values = {key: value for key, value in locals().items() if value is not Ellipsis}
        from app.modules.rss_subscriptions import apply_change
        return apply_change("create", values)

    @staticmethod
    @sdk_method('write')
    def update(subscription_id: int, *, name: str | EllipsisType = ..., urls: str | list[str] | EllipsisType = ..., exclude_keywords: str | EllipsisType = ..., action: str | EllipsisType = ..., enabled: bool | EllipsisType = ..., refresh_interval_minutes: int | EllipsisType = ..., download_method: str | EllipsisType = ..., media_tmdb_id: str | EllipsisType = ..., media_default_season: int | EllipsisType = ..., skip_existing_episodes: bool | EllipsisType = ..., parser: str | EllipsisType = ..., refresh_cron: str | EllipsisType = ..., qb_save_path: str | EllipsisType = ..., gy_target_dir: str | EllipsisType = ..., gy_target_dir_name: str | EllipsisType = ...) -> dict:
        """只修改传入字段；False、0、空字符串是有效值。工作区冻结配置后确认，不立即刷新或下载。"""
        values = {key: value for key, value in locals().items() if value is not Ellipsis}
        from app.modules.rss_subscriptions import apply_change
        return apply_change("update", values)

    @staticmethod
    @sdk_method('write')
    def delete(subscription_id: int) -> dict:
        """确认后删除RSS配置及本地条目；有运行中刷新/提交时拒绝，不删除下载任务或文件。"""
        from app.modules.rss_subscriptions import apply_change
        return apply_change("delete", {"subscription_id": subscription_id})


class Watchlist:
    """用户在探索页保存的收藏清单；与 subscriptions 自动追更订阅分开。"""
    @staticmethod
    @sdk_method('read')
    def list(limit: int = 200, before_number: int | None = None) -> dict:
        """读取探索收藏，返回 {items, total, has_more, next}，不包含订阅独有项。
        items 每项含 id/provider/external_id/media_type/title/year；作品身份是
        (provider, external_id, media_type)，id 是收藏记录编号。has_more 为真时用 next 续页。

        >>> page = watchlist.list()
        >>> identities = [(row["provider"], row["external_id"], row["media_type"]) for row in page["items"]]
        """
        from app.discovery.service import DiscoveryService

        return DiscoveryService.list_watchlist(limit=limit, before_number=before_number)

    @staticmethod
    @sdk_method('read')
    def get(watchlist_id: int) -> dict:
        """按收藏记录 id 返回 {item}；item 含 id/provider/external_id/media_type/title/year，
        记录不存在时为 None。例（收藏列表非空时）：

        >>> page = watchlist.list()
        >>> item = watchlist.get(page["items"][0]["id"])["item"]
        """
        from app.discovery.service import DiscoveryService

        return DiscoveryService.get_watchlist(watchlist_id)

    @staticmethod
    @sdk_method('read')
    def statistics() -> dict:
        """统计全部收藏数量及各类型数量、最新三项；total 是全库总数，不是列表页长。"""
        from app.discovery.service import DiscoveryService

        return DiscoveryService.watchlist_statistics()

    @staticmethod
    @sdk_method('write')
    def add(provider: str, media_type: str, external_id: str) -> dict:
        """将已核实的作品身份加入探索收藏。工作区生成确认卡；已收藏时不重复写入。这是收藏，不是自动追更订阅。"""
        from app.discovery.models import MediaCard
        from app.discovery.service import get_discovery_service

        service = get_discovery_service()
        prepared = service.prepare_watchlist_add(provider, media_type, external_id)
        if prepared is None:
            return {"ok": False, "status": "not_found"}
        if not prepared["expected_absent"]:
            return {"ok": True, "status": "no_changes"}
        identity = service.add_watchlist(MediaCard(**prepared["canonical"]), expected_absent=True)
        return {"ok": identity is not None, "status": "success" if identity is not None else "conflict", "id": identity}

    @staticmethod
    @sdk_method('write')
    def remove(watchlist_id: int) -> dict:
        """按 watchlist.list 返回的 id 移除收藏；工作区确认后只删除预览中的记录，不删除媒体或取消订阅。"""
        from app.discovery.service import DiscoveryService

        snapshot = DiscoveryService.get_watchlist_snapshot(watchlist_id)
        if not snapshot["exists"]:
            return {"ok": True, "status": "no_changes"}
        removed = DiscoveryService.remove_watchlist(
            snapshot["provider"], snapshot["media_type"], snapshot["external_id"], expected_snapshot=snapshot,
        )
        return {"ok": removed, "status": "success" if removed else "conflict", "id": watchlist_id}


class Settings:
    """项目的非敏感白名单功能开关与策略。"""
    @staticmethod
    @sdk_method('write')
    def test_notification() -> dict:
        """确认后向已配置的Telegram会话发送一条固定连接测试；不接受任意消息或收件人，超时不代表未送达。"""
        raise ValueError("通知测试须通过工作区的 EffectExecutor 确认执行器调用")

    @staticmethod
    @sdk_method('read')
    def policies() -> dict:
        """读取13项非敏感策略的当前值、显示值和部署托管标记，不返回凭据或任意配置。"""
        from app.modules.policy_config import policies
        return policies()

    @staticmethod
    @sdk_method('write')
    def set_policy(policy: str, value: str | int) -> dict:
        """确认后修改一项白名单策略；枚举值用字符串，数值策略用整数。保留部署覆盖与缓存TTL关系，TMDB壁纸需要已配置密钥；相同值无需写入。"""
        raise ValueError("策略写入须通过工作区的 EffectExecutor 确认执行器调用")

    @staticmethod
    @sdk_method('read')
    def features() -> dict:
        """完整读取11项白名单功能的真实启停、依赖可用性、环境托管标记及安全依赖计数。不返回user.env、配置键、路径、站点或密钥，不访问网络。enabled为已配置状态，availability说明依赖是否允许当前使用。"""
        from app.modules.feature_config import features

        return features()

    @staticmethod
    @sdk_method('write')
    def set_feature(feature: str, enabled: bool) -> dict:
        """确认后单项设置功能。feature仅支持discovery/douban/resource_results/indexer_search/web_search/web_read/offline_magnet/offline_ed2k/offline_http/strm_metadata/download_verification_notify，enabled必须为bool。不修改部署覆盖、Agent权限或任意配置；同状态无需确认。使用配置CAS和Web相同reload；保存或刷新结果未知先读取核验，不重放脚本或旧卡。"""
        raise ValueError("功能开关写入须通过工作区的 EffectExecutor 确认执行器调用")


class Recognition:
    """文件名解析、发布格式教学和发布组/尾部制作组知识库；知识库不是 TMDB 身份映射。"""
    @staticmethod
    @sdk_method('read')
    def rules(rule_type: str) -> dict:
        """只读完整列出preprocess_rule/tmdb_regex_rule/release_format的真实id、启停状态和规则内容，格式另含revision；含启用与停用行，不初始化种子、不返回临时编号。不同类型ID可重号，启停必须同时指定类型。知识请用knowledge读取。"""
        from app.modules.recognition_service import rules

        return rules(rule_type)

    @staticmethod
    @sdk_method('write')
    def set_rule_enabled(rule_type: str, rule_id: int, enabled: bool) -> dict:
        """仅按rules返回的真实类型和id启停一条预处理、TMDB正则或发布格式规则。工作区先确认，事务内比较业务行；格式启用复核当前注册表与样本冲突。不修改规则内容、TMDB、优先级或媒体。knowledge_entry不在此接口，知识启停统一用update_knowledge(entry_id, disabled=not enabled)。"""
        raise ValueError("识别规则启停须通过工作区的 EffectExecutor 确认执行器调用")

    @staticmethod
    @sdk_method('read')
    def knowledge(keyword: str = "", knowledge_type: str = "", limit: int = 300, offset: int = 0) -> dict:
        """完整读取识别知识事实（含来源、证据、置信度、统计和时间）。items[].id 就是真实 entry_id；不使用临时编号。默认300、最多500条，total为筛选后总数，summary是全库统计；has_more时用next_offset继续，保持原筛选。分页期间业务变化可重新读取，不把单页当全库。"""
        from app.modules.recognition_knowledge import list_entries

        return list_entries(keyword=keyword, knowledge_type=knowledge_type, limit=limit, offset=offset)

    @staticmethod
    @sdk_method('write')
    def create_knowledge(knowledge_type: str, canonical_value: str, *, aliases: list[str] | EllipsisType = ..., disabled: bool = False) -> dict:
        """新增用户发布组/尾部制作组知识及别名；工作区生成确认卡，确认后复用公共CRUD。不可指定来源、证据、知识键或置信度；不整理文件。"""
        raise ValueError("识别知识写入须通过工作区的 EffectExecutor 确认执行器调用")

    @staticmethod
    @sdk_method('write')
    def update_knowledge(entry_id: int, *, knowledge_type: str | EllipsisType = ...,
                         canonical_value: str | EllipsisType = ..., aliases: list[str] | EllipsisType = ...,
                         disabled: bool | EllipsisType = ...) -> dict:
        """按knowledge返回的真实id修改名称、类型、别名或停用状态。未传字段保留，空别名数组/False有效；不可改来源、证据、知识键或置信度。确认后事务内比较业务行；计数更新不使确认失效。"""
        raise ValueError("识别知识写入须通过工作区的 EffectExecutor 确认执行器调用")

    @staticmethod
    @sdk_method('write')
    def delete_knowledge(entry_id: int) -> dict:
        """按knowledge返回的真实id删除用户或学习知识；须确认，内置词条只能停用。业务行变化时拒绝旧卡，不删除媒体。"""
        raise ValueError("识别知识写入须通过工作区的 EffectExecutor 确认执行器调用")

    @staticmethod
    @sdk_method('read')
    def inspect_filenames(filenames: list[str], parent_path: str = "") -> dict:
        """批量解析1–100个用户真实文件名，返回全部rows、标题变体、年份、原始发布季集、目录上下文季集及清洗证据。用户问如何识别/清洗时先调用，不要求模板或标注答案。parent_path仅用用户提供的目录文本，不猜目录、不读写文件、不查TMDB、不保存规则；未知季集保持空值，发布季集不等于TMDB映射，标题变体不是已核验译名。"""
        from app.modules.recognition_service import inspect_filenames

        return inspect_filenames(filenames, parent_path)

    @staticmethod
    @sdk_method('read')
    def preview_release_format(draft: dict, examples: list[dict], filenames: list[str] | None = None) -> dict:
        """用户明确要求教学时只读完整批量预览，不创建保存确认。draft含name/template/scope/parent_path；由你生成受限模板，不要要求用户写模板。仅{title}/{episode}必填，{season}/{version}/{resolution}/{checksum}可选，其余字面匹配并保留分隔符/视频扩展名。examples须2–8个用户明确标注的不同真实文件，每项含filename/title/episode及可选season，至少两个不同原始集号；标题为文件中的片名，不编造标签/译名/偏移。缺少信息先询问真实文件名、正确标题/集号和父目录。默认directory；parent_path按整理起点目录名/起点下的相对父目录，不能用来源显示别名或直接照抄绝对路径，保留已有规则原值。release跨目录需用户授权、固定发布前缀、不同标题样本，parent_path为空。filenames最多100个。返回全部examples/rows/summary/warnings/can_save；报告真实标题/季集与冲突，不展示模板或票据，不将只读预览转为保存。"""
        from app.modules.recognition_service import preview_release_format

        return preview_release_format(draft, examples, filenames)

    @staticmethod
    @sdk_method('write')
    def save_release_format(draft: dict, examples: list[dict], filenames: list[str] | None = None) -> dict:
        """仅用户明确要求记住/复用发布格式时创建保存确认；参数约束与preview_release_format完全一致，必须用户明确样本，不编造标签，不改变已授权模板/范围。工作区由EffectExecutor冻结原预览、用户确认后一次保存；取消、配置或规则快照变化均不能写入。只影响以后识别，不移动文件、不绑定TMDB、不偏移季集；不是知识库或规则启停。保存成功前不能宣称已学会。"""
        raise ValueError("保存发布格式须通过工作区的 EffectExecutor 确认执行器调用")


class Library:
    """Jellyfin/Emby 连接身份、本地媒体库搜索、库存、缺集、观看历史与推荐、用户状态和播放列表。"""
    @staticmethod
    @sdk_method('read')
    def path_mappings(server: str) -> dict:
        """完整读取指定jellyfin/emby槽位的路径对items[].local/server及部署覆盖状态；不探测上游。以server与local的自然路径键定位，不使用列表序号。不是本地归档绑定，不读取凭据。"""
        from app.modules.media_server_path_mapping import list_path_mappings

        return list_path_mappings(server)

    @staticmethod
    @sdk_method('write')
    def create_path_mapping(server: str, local_path: str, server_path: str) -> dict:
        """新增本地到媒体服务器可见路径的映射；显式指定jellyfin/emby，工作区生成确认卡，确认后沿公共配置CAS保存。不改其它服务器、凭据、归档绑定或全局刷新策略，不移动文件。"""
        raise ValueError("路径映射写入须通过工作区的 EffectExecutor 确认执行器调用")

    @staticmethod
    @sdk_method('write')
    def update_path_mapping(server: str, local_path: str, *, new_local_path: str | EllipsisType = ...,
                            server_path: str | EllipsisType = ...) -> dict:
        """用path_mappings返回的原local路径定位；new_local_path改本地前缀，server_path改服务器前缀，未传字段保留。Windows/UNC按原规范比较大小写，POSIX区分大小写。确认后仅在原配置未变时保存，不靠重排后的序号定位。"""
        raise ValueError("路径映射写入须通过工作区的 EffectExecutor 确认执行器调用")

    @staticmethod
    @sdk_method('write')
    def delete_path_mapping(server: str, local_path: str) -> dict:
        """确认后按显式服务器和原local路径删除一条映射；只改配置，不删除媒体或STRM，不启动刷新。配置变化须重新预检，取消或旧卡重放不会写入。"""
        raise ValueError("路径映射写入须通过工作区的 EffectExecutor 确认执行器调用")

    @staticmethod
    def _server(server: str) -> str:
        if server == "auto" and _CALL_OWNER.get():
            preferred = preferences.get()["preferred_server"]
            if preferred != "any":
                return preferred
        return server

    @staticmethod
    @sdk_method('read')
    def connection(server: str = "all") -> dict:
        """只读探测当前配置的固定媒体服务器；server为all/jellyfin/emby，默认全部，忽略首选服务器偏好。返回各槽位的连通性、脱敏server_name/product_name、已识别product、version/major_version、compatibility、reason_code及安全error。版本严格归类，缺失或无法确认的身份为None/unknown，不从配置槽位猜产品。disabled/not_configured不发请求；鉴权、重定向、超时、异常响应分别报告，禁止携带凭据跟随重定向。probe_complete=False时未确认的计数保持None；失败不会当成空结果。"""
        from app.modules.media_server_profiles import connection
        return connection(server)

    @staticmethod
    @sdk_method('read')
    def user_state(item_id: str, server: str = "auto") -> dict:
        """按真实item_id读取当前配置/默认用户的已看、收藏、播放计数和进度；缺失字段为None。auto采用当前身份首选服务器；结果保留真实ID和用户scope。"""
        from app.modules.media_user import user_state
        return user_state(item_id, Library._server(server))

    @staticmethod
    @sdk_method('read')
    def playlists(server: str = "auto", offset: int = 0, limit: int = 20) -> dict:
        """分页列出当前用户可访问的播放列表；返回真实id、total、has_more、next_offset及用户scope。offset非负、limit为1–250；total未知为None；可访问不证明所有权，缺失的公开/所有者字段保持None。auto采用首选服务器。"""
        from app.modules.media_user import playlists
        return playlists(Library._server(server), offset=offset, limit=limit)

    @staticmethod
    @sdk_method('read')
    def playlist(playlist_id: str, server: str = "auto", offset: int = 0, limit: int = 250) -> dict:
        """读取真实播放列表及成员item_id/entry_id；默认完整返回250项以内成员，超过范围按next_offset续页。完整快照才can_modify；写入校验最多250成员。列表对当前用户可访问，不等于私有或本人所有；未知归属保持None。auto采用首选服务器。"""
        from app.modules.media_user import playlist
        return playlist(playlist_id, Library._server(server), offset=offset, limit=limit)

    @staticmethod
    @sdk_method('write')
    def set_played(item_id: str, played: bool = True, server: str = "auto") -> dict:
        """确认后修改真实item_id的当前用户已看状态；played必须布尔，False表示未看。仅单个可播放条目，不展开剧集。冻结相关状态、名称、类型及用户，变化拒绝写入；修改可能调整播放计数/进度，回读验证结果。auto采用首选服务器。"""
        raise ValueError("媒体状态变更须通过工作区EffectExecutor确认执行")

    @staticmethod
    @sdk_method('write')
    def set_favorite(item_id: str, favorite: bool = True, server: str = "auto") -> dict:
        """确认后修改真实item_id的当前用户收藏状态；favorite必须布尔，False表示取消。冻结收藏、条目及用户范围，普通播放进度变化不使收藏确认失效；写后回读。auto采用首选服务器。"""
        raise ValueError("媒体状态变更须通过工作区EffectExecutor确认执行")

    @staticmethod
    @sdk_method('write')
    def create_playlist(name: str, item_ids: list[str], server: str = "auto") -> dict:
        """确认后为明确配置用户创建Jellyfin私有Video列表；name为1–120字符，item_ids为1–20个不同的真实单视频ID，同名列表拒绝创建。Emby凭据无法验证所有者时明确不支持。写后验证私有性与完整成员；返回真实playlist_id和entry_id。auto采用首选服务器。"""
        raise ValueError("播放列表变更须通过工作区EffectExecutor确认执行")

    @staticmethod
    @sdk_method('write')
    def add_to_playlist(playlist_id: str, item_ids: list[str], server: str = "auto") -> dict:
        """确认后将1–20个真实单视频ID加入当前用户可访问列表，跳过已有媒体。冻结完整成员和用户，变更后最多250项；并发变化拒绝写入，回读返回完整成员及真实entry_id。列表可能共享；不删除媒体文件。auto采用首选服务器。"""
        raise ValueError("播放列表变更须通过工作区EffectExecutor确认执行")

    @staticmethod
    @sdk_method('write')
    def remove_from_playlist(playlist_id: str, entry_ids: list[str], server: str = "auto") -> dict:
        """确认后按1–20个真实entry_id移出列表成员，不接受媒体item_id代替；成员必须属于该列表。冻结完整成员和用户，最多250项；并发变化拒绝写入，回读返回剩余完整成员。列表可能共享；不删除媒体文件。auto采用首选服务器。"""
        raise ValueError("播放列表变更须通过工作区EffectExecutor确认执行")

    @staticmethod
    @sdk_method('read')
    def libraries(server: str = "auto") -> dict:
        """列出真实媒体库 id/name/collection_type，不返回磁盘路径。auto使用已保存的首选服务器；未指定且有多个可用源时需明确 jellyfin/emby。"""
        from app.modules.media_library import libraries
        return libraries(Library._server(server))

    @staticmethod
    @sdk_method('read')
    def recent(server: str = "auto", limit: int = 20, library_id: str = "", library_name: str = "", media_type: str = "all") -> dict:
        """读取最近入库，保留真实条目ID。limit为1–200；最多扫描2000条，不代表全库库存。library_id/library_name二选一；库名精确匹配，缺失/同名不会退回全库。all/movie/tv按作品去重，episode按单集保留同剧多集；tv不排除动漫。auto遵循首选服务器。"""
        from app.modules.media_library import recent
        return recent(Library._server(server), limit=limit, library_id=library_id,
                      library_name=library_name, media_type=media_type)

    @staticmethod
    @sdk_method('read')
    def history(server: str = "auto", limit: int = 8) -> dict:
        """读取真实播放事件/DatePlayed；不能用继续观看列表代替。limit为1–50；保留完整条目、真实ID及用户范围。count仅为本次返回数；状态按本次窗口为success/empty，total/has_more/complete未知为None，不推算全部记录。auto采用当前身份已存首选服务器；用户优先配置、否则服务器默认。"""
        from app.modules.media_library import history
        return history(Library._server(server), limit=limit)

    @staticmethod
    @sdk_method('read')
    def resume(server: str = "auto", limit: int = 8) -> dict:
        """读取当前配置/默认用户的继续观看进度，limit为1–20；不是播放历史。保留真实ID、完整条目和用户范围；count仅为返回数，状态按本次窗口为success/empty，未知total/has_more/complete保持None。auto采用已存首选服务器；可用时提供已校验的open_url。"""
        from app.modules.media_library import resume
        return resume(Library._server(server), limit=limit)

    @staticmethod
    @sdk_method('read')
    def recommend(server: str = "auto", media_type: str = "any", must_match: list[str] | EllipsisType = ...,
                  prefer: list[str] | EllipsisType = ..., exclude: list[str] | EllipsisType = ...,
                  min_rating: float | EllipsisType = ..., exclude_played: bool | EllipsisType = ..., limit: int = 8) -> dict:
        """从本地Jellyfin/Emby库分页取最多5000个movie/tv候选，结合50条真实历史排名；limit为1–20，仅限制返回作品数。must_match最多6个必须同时满足的概念，同义词用|合并；prefer最多12项，只用于排序；exclude最多8项，命中即排除；min_rating为0–10。省略偏好/排除/最低评分/已看过滤采用当前身份已存偏好，显式[]/0/False优先；auto采用已存首选服务器。未知总数保持None，库存截断或总数未知为partial；匹配和排除计数只覆盖已扫描范围，tv不等于某个媒体库。历史失败会报错。结果保留真实ID、客户端提供的完整元数据和用户范围；可用时提供已校验的open_url。"""
        arguments = {key: value for key, value in locals().items() if value is not Ellipsis}
        from app.modules.media_library import recommend
        return recommend(**arguments, preferences=preferences.get() if _CALL_OWNER.get() else None,
                         cancelled=_CALL_CANCELLED.get())

    @staticmethod
    @sdk_method('read')
    def quality(server: str, library_id: str, start_index: int = 0, limit: int = 20,
                min_resolution: int = 1080, subtitle_language: str = "chinese") -> dict:
        """按真实库ID分页检查分辨率/字幕/版本，未知字段保持未知。next_start_index续页；重复检测仅本页，不播放或探测STRM。"""
        from app.modules.media_library import quality
        return quality(Library._server(server), library_id, start_index=start_index, limit=limit,
                       min_resolution=min_resolution, subtitle_language=subtitle_language)

    @staticmethod
    @sdk_method('write')
    def refresh(server: str, library_id: str, *, mode: str = "scan", replace_images: bool = False,
                regenerate_trickplay: bool = False) -> dict:
        """直接刷新指定真实媒体库，无需确认卡。mode=scan扫描新增/修改（默认）、missing补齐缺失元数据、replace覆盖元数据；后两种模式可独立开启replace_images替换图片、regenerate_trickplay重建Jellyfin进度条预览图，默认均关闭。仅在用户要求时覆盖；accepted仅表示已排队，不是完成，结果未知不重发。"""
        from app.modules.media_library import prepare_refresh, refresh
        options = dict(mode=mode, replace_images=replace_images, regenerate_trickplay=regenerate_trickplay)
        prepared = prepare_refresh(Library._server(server), library_id, **options)
        return refresh(prepared["server"], library_id, expected_snapshot=prepared["snapshot"], **options)

    @staticmethod
    @sdk_method('read')
    def patrol_policy() -> dict:
        """读取全库缺集巡检策略与环境托管标记。只读，不运行任务。"""
        from app.modules.schedule_policy import summarize_policy

        return summarize_policy("patrol")

    @staticmethod
    @sdk_method('write')
    def set_patrol_policy(*, enabled: bool | EllipsisType = ..., notify_enabled: bool | EllipsisType = ..., interval_hours: int | EllipsisType = ..., max_series: int | EllipsisType = ...) -> dict:
        """修改全库缺集巡检策略的指定字段，未传字段保留；工作区先预览确认。保存后只安排后续任务，不立即执行或中断运行。环境托管或并发修改会拒绝覆盖；runtime_refreshed=false表示配置已保存但需重启应用。关闭通知会丢弃尚未发送的通知积压，无法恢复。"""
        changes = {name: value for name, value in locals().items() if value is not Ellipsis}
        from app.modules.schedule_policy import prepare_policy_change, apply_policy_change
        preview, fingerprint = prepare_policy_change("patrol", changes)
        if preview["status"] != "confirmation_required":
            return preview
        return apply_policy_change("patrol", changes, expected_fingerprint=fingerprint)

    @staticmethod
    @sdk_method('read')
    def counts(server: str = "all", library_id: str = "") -> dict:
        """实时全库/指定库统计。server 为 all/jellyfin/emby；指定真实 library_id 必须明确服务器。
        counts 每个字段仅在全部来源该字段已知时为完整计数；partial 用 known_counts 和 sources 保留已知范围。
        多源统计是服务器记录数之和，不按作品去重；零计数可成功，未知为 None。
        """
        from app.services import get_library_counts

        return get_library_counts(server, library_id)

    @staticmethod
    @sdk_method('read')
    def search(query: str, search_by: str = "title", media_type: str = "all",
               count_only: bool = False, limit: int = 50, offset: int = 0, server: str = "all") -> dict:
        """按 title 或 actor 元数据搜索，media_type 为 all/movie/series/episode。
        server 为 all/jellyfin/emby，默认查询全部已配置源；limit 为每源 1–50 条，offset 为非负整数。
        返回 {ok, status, data}：data.total 为服务端记录数之和（不去重），未知为 None；
        data.known_total 保留已知计数。count_only=True 只取计数，不需逐页累加。
        data.sources 保留每源 total/returned/next_offset/items；items 含真实媒体 ID 和 TMDB 映射。
        offset 逐源续页；返回条数或 limit 不代表命中总数。
        """
        from app.services import search_library

        return search_library(query, search_by=search_by, media_type=media_type,
                              count_only=count_only, limit=limit, offset=offset, **({"server": server} if server != "all" else {}))

    @staticmethod
    @sdk_method('read')
    def presence(items: list[dict]) -> dict:
        """批量排库，items 每项必需 tmdb_id 和 media_type(movie/tv)，title/year 可选。
        可直接传入含 TMDB 身份的完整媒体 items；其余描述字段和已有库存状态不参与核验。
        一次共用库存读取；缺标题时只核对 ID，未映射库存或不完整库存保留未知，不当缺失。
        返回 {ok, status, data}，data 含 items/total/sources，完成核验时另含 counts；每项含输入身份和
        library_status（present/possible/missing/indeterminate），只有 missing 表示已核验不在库。

        >>> result = library.presence([{"tmdb_id": "12345", "media_type": "movie"}])
        >>> missing = [item for item in result["data"]["items"] if item["library_status"] == "missing"]
        """
        from app.services import batch_library_presence

        return batch_library_presence(items, cancelled=_CALL_CANCELLED.get())

    @staticmethod
    @sdk_method('read')
    def episodes(query: str = "", tmdb_id: str = "", library_name: str = "", *,
                 series_id: str = "", server: str = "auto", max_episodes: int = 2000, include_specials: bool = False) -> dict:
        """只读本地库存，两种选择方式互斥，均返回 {ok, status, data}。
        query/tmdb_id 至少一项，library_name 可限定库；data 含 local_episode_count、
        seasons（每项 season/count/episode_ranges）、inventory_complete 和 sources。
        episode_ranges 保留断号；未命中或失败时计数为 None，可能没有 seasons。
        series_id 配合 server 读取明细；data.episodes 每项为 {season, episode}，
        data 另含 count/total/inventory_complete/truncated。count 为去重季集数；
        total 为客户端观测文件记录数，截断时可能只是下界，未知为 None。
        max_episodes（1–2000）与 include_specials 仅用于 series_id；inventory_complete 表示是否读全。

        >>> summary = library.episodes(tmdb_id="12345")["data"]
        >>> ranges = [(row["season"], row["episode_ranges"]) for row in summary.get("seasons", [])]
        >>> detail = library.episodes(series_id="series-id", server="jellyfin")["data"]
        >>> episodes = [(row["season"], row["episode"]) for row in detail["episodes"]]
        """
        if series_id:
            if query or tmdb_id or library_name:
                raise ValueError("series_id 不能与 query/tmdb_id/library_name 混用")
            from app.modules.media_library import series_episodes
            return series_episodes(Library._server(server), series_id, max_episodes=max_episodes, include_specials=include_specials)
        if server != "auto" or max_episodes != 2000 or include_specials:
            raise ValueError("server/max_episodes/include_specials 仅用于真实 series_id 读取")
        from app.services import count_series_episodes

        return count_series_episodes(query, tmdb_id=tmdb_id, library_name=library_name)

    @staticmethod
    @sdk_method('write')
    def start_audit(as_of: str | None = None, max_series: int = 50) -> dict:
        """确认后创建当前用户的后台全库缺集审计，返回真实job_id。as_of默认预检当日；max_series为每批1至100部，默认50，不是全库总上限。同用户/日期/批大小的活动任务复用；任务支持重启恢复，不下载或修改媒体。accepted仅表示持久队列已受理。"""
        raise ValueError("后台全库审计须通过工作区确认执行")

    @staticmethod
    @sdk_method('read')
    def audit_job(job_id: str | None = None, limit: int = 5) -> dict:
        """读取当前用户指定真实job_id的状态；省略ID读取最近limit条（1至10）。task_status是队列状态，status是公开审计结论。data保留聚合计数、coverage_complete/projection_valid、findings完整度和有界样本；持久样本最多20项、每项20集，不能补读未保存明细。progress_total=None表示总进度未知；scope.jobs_has_more表示最近窗口还有任务，截断时jobs_total未知。未找到为not_run，失败及损坏投影不作为空成功。"""
        from app.modules.library_audit_jobs import audit_job

        return audit_job(owner=_owner(), job_id=job_id, limit=limit)

    @staticmethod
    @sdk_method('write')
    def cancel_audit(job_id: str | None = None) -> dict:
        """确认后取消当前用户的指定后台审计；省略ID在预检时冻结最近活动任务的真实ID。未开始任务立即取消，运行任务在有界批次结束后停止。已保存只读结果保留，不删除媒体，不改其他任务。"""
        raise ValueError("取消后台全库审计须通过工作区确认执行")

    @staticmethod
    @sdk_method('read')
    def audit(
        query: str, tmdb_id: str = "", season: int | None = None,
        target_episode: int | None = None, library_name: str = "",
        as_of: str | None = None, refresh: bool = False,
    ) -> dict:
        """按非空 query 比较库存与已播集，返回 {ok, status, checked_at, data, mapping_context, ...}。
        data 含 local_episode_count/local_inventory_complete、missing_count、sources；
        完成对照时 missing_sample 为完整 {season, episode} 列表，missing_sample_truncated=False，
        episodes 每项含 season/episode/aired/local。未能对照时 missing_count 为 None，可能没有这些列表。
        mapping_context 在根级，指定 season 时附季序映射，未取得映射时可为空；完整结果可供 rank_episode 使用。

        >>> result = library.audit("目标剧", tmdb_id="12345", season=1)
        >>> missing = [(row["season"], row["episode"]) for row in result["data"].get("missing_sample", [])]
        >>> mapping = result["mapping_context"]
        """
        from app.modules.episode_audit import audit_series_episodes

        arguments: dict[str, Any] = {
            "query": query, "tmdb_id": tmdb_id, "season": season,
            "library_name": library_name,
        }
        if target_episode is not None:
            arguments["target_episode"] = target_episode
        if as_of is not None:
            arguments["as_of"] = as_of
        return asdict(audit_series_episodes(arguments, refresh=refresh))

    @staticmethod
    @sdk_method('read')
    def updates(query: str | None = None, queries: list[str] | None = None, media_type: str = "auto",
                tmdb_id: str = "", season: int | None = None, as_of: str | None = None, refresh: bool = True) -> dict:
        """更新核对，提供非空 query 或非空 queries 列表；queries 不能同时传 query/tmdb_id/season。
        单项返回 {ok, status, checked_at, data, ...}：TV 的 data 与 audit 相同；
        电影 data 含 local_match_status/comparison/sources，只核对本地存在性，不证明版本更优。
        批量返回 data.counts 与 data.items，按输入去重后的顺序排列；每项直接含
        query/title/ok/status/missing_count/missing_sample/sources，未知计数为 None。
        默认刷新，保留未知与部分来源，不自动搜索或下载资源。

        >>> single = library.updates(query="目标剧", media_type="tv")
        >>> missing = single["data"].get("missing_sample", [])
        >>> batch = library.updates(queries=["目标剧"], media_type="tv")
        >>> rows = [(row["query"], row["missing_count"], row["missing_sample"]) for row in batch["data"]["items"]]
        """
        from app.modules.episode_audit import check_library_updates

        arguments: dict[str, Any] = {"media_type": media_type, "as_of": as_of, "refresh": refresh}
        if query is not None:
            arguments["query"] = query
        if queries is not None:
            arguments["queries"] = queries
        if tmdb_id != "":
            arguments["tmdb_id"] = tmdb_id
        if season is not None:
            arguments["season"] = season
        return check_library_updates(arguments, cancelled=_CALL_CANCELLED.get(), checkpoint=_READ_CHECKPOINT.get())

    @staticmethod
    @sdk_method('read')
    def audit_all(as_of: str | None = None, max_series: int = 50, after_tmdb_id: str = "") -> dict:
        """分批核对全库剧集。continuation_pending时用last_processed_tmdb_id续查；保留请求预算、失败来源和未知播出日期，不能把部分核对当全库完成。"""
        from app.modules.episode_audit import audit_library_episodes_batch

        return asdict(audit_library_episodes_batch({
            "as_of": as_of, "max_series": max_series, "after_tmdb_id": after_tmdb_id,
        }))


    @staticmethod
    @sdk_method('read')
    def patrol_status(run_id: int | None = None) -> dict:
        """读取后台全库巡检状态与缺集摘要；传 run_id 查看指定一次运行，未传则查看最近状态。不会触发巡检或下载。"""
        from app.modules.library_patrol import patrol_status

        return patrol_status(run_id)

    @staticmethod
    @sdk_method('write')
    def run_patrol() -> dict:
        """确认后按当前巡检策略立即排队，已运行时复用该次 run_id。返回 accepted 表示受理；用 patrol_status(run_id) 查询结果。不修改策略、不搜索或下载。"""
        raise ValueError("立即巡检须通过工作区确认执行")


class Media:
    """作品元数据、榜单、排期及媒体服务器单项刷新。"""
    @staticmethod
    @sdk_method('write')
    def refresh(item_id: str, server: str = "auto", *, mode: str = "scan", replace_images: bool = False,
                regenerate_trickplay: bool = False) -> dict:
        """直接刷新指定真实电影/整部剧/季/单集，无需确认卡。mode=scan扫描新增/修改（默认）、missing补齐缺失元数据、replace覆盖元数据；后两种模式可独立开启replace_images替换图片、regenerate_trickplay重建Jellyfin进度条预览图，默认均关闭。仅在用户要求时覆盖；accepted仅表示已排队，不是完成，结果未知不重发。"""
        raise ValueError("媒体刷新须通过工作区执行")

    @staticmethod
    @sdk_method('read')
    def people(query: str, page: int = 1) -> dict:
        """按姓名发现TMDB人物候选，items保留完整源页及id/name/known_for_department/known_for等事实，不自动按热度选人。返回page/total_pages/total/returned/has_more/next_page；需要时按next_page续读。核对候选真实id后调用media.filmography(person_id)，歧义时保留候选请用户辨别；query_state区分未查询/失败/空页。"""
        from app.discovery.people import people

        return people(query, page=page)

    @staticmethod
    @sdk_method('read')
    def credits(tmdb_id: str, media_type: str, season_number: int | None = None,
                limit: int | None = None, offset: int = 0) -> dict:
        """按真实TMDB作品ID查询完整演职员。movie/全剧tv/season_number含0特别篇；返回cast/crew原始角色表、各自total/returned和has_more/next_offset。默认全表及全部roles/jobs；显式limit分页可续读。query_state区分not_queried/failed/empty/available/partial；空表仅是本次TMDB结果。"""
        from app.discovery.people import credits

        return credits(tmdb_id, media_type, season_number=season_number, limit=limit, offset=offset)

    @staticmethod
    @sdk_method('read')
    def filmography(person_id: str, role: str = "all", include_upcoming: bool = True,
                    limit: int | None = None, offset: int = 0) -> dict:
        """按明确TMDB人物ID读取完整电影作品与角色，人物ID先用media.people(query)核实，或取credits中的id；不按姓名/热度自动选人。默认全部角色及未来/未知日期作品；items含完整电影字段/年份/roles；library_check_items为同一页的四字段身份，可直接交library.presence(result["library_check_items"])。返回total/returned/has_more/next_offset，role可all/acting/directing/writing/production；limit=None完整读取。"""
        from app.discovery.people import filmography

        return filmography(person_id, role=role, include_upcoming=include_upcoming, limit=limit, offset=offset)

    @staticmethod
    @sdk_method('read')
    def search(query: str, page: int = 1, providers: list[str] | None = None, *,
               media_type: str = "", year: str = "", region: str = "", genre: str = "") -> dict:
        """按片名搜索公网作品身份，返回完整当前页items/has_more/providers_attempted/providers_succeeded/errors/status/partial。media_type/year精确过滤本页；region/genre只作查询关键词，不保证硬性筛选。资源版本和磁力检索使用resources.search。"""
        from app.discovery.search import get_discovery_search_service
        from app.discovery.service import require_discovery_enabled

        require_discovery_enabled()
        return get_discovery_search_service().search(
            query, page, providers, media_type=media_type, year=year, region=region, genre=genre,
            cancelled=_CALL_CANCELLED.get(),
        ).to_dict()

    @staticmethod
    @sdk_method('read')
    def sources() -> dict:
        """catalogs 列出全部可查询的 provider/category/media_types；supports_collection 表示可传 limit 收集推荐。
        sections 仅为探索首页展示栏目，不是全部查询能力；enabled 表示来源开关。
        """
        from app.discovery.registry import list_catalog_definitions
        from app.discovery.service import get_discovery_service

        return {"sections": get_discovery_service().list_sections(), "catalogs": list_catalog_definitions()}

    @staticmethod
    @sdk_method('read')
    def filters(provider: str, media_type: str) -> dict:
        """返回适用的 category、枚举 filters、自由填写的 extra_filters（含格式/示例）与 defaults。
        按 key 组装 catalog 的 filters；产地与原始语言不同，日期/年份字段按 movie/tv 区分。
        """
        from app.discovery.service import DiscoveryService

        return DiscoveryService.list_filters(provider, media_type)

    @staticmethod
    @sdk_method('read')
    def catalog(provider: str, category: str, media_type: str, page: int = 1, filters: dict | None = None, *,
                limit: int | None = None, offset: int = 0, year: str = "", year_from: str = "", year_to: str = "",
                region: str = "", genre: str = "", exclude_library: bool = False) -> dict:
        """默认返回完整单页items/page/has_more/cached/stale/provider。
        items 每项含 title/year/release_date/tmdb_id/media_type/origin_country/genre_ids/rating。
        vote_count 为来源评分人数，popularity 为来源热度，两者缺失时为 None，热度不是评分。
        显式limit正整数收集tmdb/discover或douban/recommend：去重、精确年份、产地/题材筛选；
        exclude_library只保留已核验不在库者，并在items各项标记library_status=missing。
        收集结果另含status/partial/library_check/scope；library_check含全部已核对候选，
        推荐条目取顶层items，未取完按scope.next_page/next_offset续取。
        Bangumi周历用bangumi/calendar/tv，filters.weekday为1–7，不传取全周；三平台动漫排期用media.calendar。
        """
        from app.discovery.service import get_discovery_service, require_discovery_enabled

        require_discovery_enabled()
        if limit is not None:
            from app.services import collect_discovery_recommendations

            return collect_discovery_recommendations(
                get_discovery_service, provider=provider, category=category, media_type=media_type,
                page=page, filters=filters, limit=limit, offset=offset, year=year, year_from=year_from,
                year_to=year_to, region=region, genre=genre, exclude_library=exclude_library,
                cancelled=_CALL_CANCELLED.get(),
            )
        if offset or year or year_from or year_to or region or genre or exclude_library:
            raise ValueError("收集筛选需要显式 limit；单页筛选请使用 filters")
        return get_discovery_service().list_items(provider, category, media_type, page, filters).to_dict()

    @staticmethod
    @sdk_method('read')
    def detail(provider: str, media_type: str, external_id: str) -> dict:
        """按 provider/media_type/external_id 读取详情，返回 {item}；不存在时 item 为 None，来源异常返回错误。
        item 内含作品身份、mapping_confirmed、number_of_seasons/number_of_episodes/seasons；
        季集字段为 None 表示未知，季号 0 为特别篇，采用默认季序（非发布组或 Episode Groups 季序）。
        item.credits_state=not_queried 表示未查演员。

        >>> item = media.detail("tmdb", "tv", "12345")["item"]
        >>> seasons = item["seasons"] if item is not None else None
        """
        from app.discovery.service import get_discovery_service, require_discovery_enabled

        require_discovery_enabled()
        return {"item": get_discovery_service().describe_media(provider, media_type, external_id)}

    @staticmethod
    @sdk_method('read')
    def rating(query: str, media_type: str = "", year: str | int = "", *,
               allow_web_fallback: bool = True) -> dict:
        """按片名、movie/tv及年份查豆瓣评分。优先结构化搜索/详情，allow_web_fallback控制是否补查公开网页；返回rating及实际来源，失败或身份不明时rating为null，不猜分数。同名作品应给出类型和年份。"""
        from app.discovery.ratings import lookup_rating

        return lookup_rating(query, media_type, year, allow_web_fallback=allow_web_fallback)

    @staticmethod
    @sdk_method('read')
    def calendar(refresh: bool = False) -> dict:
        """返回上海当前周完整 days 与 sources/unscheduled；等待平台排期，不截前20条。
        days[] 含 date、weekday（1–7）、label 和 items；items[] 含 title、source 和 events。
        items[].origin_country 为 TMDB 产地代码列表；null 表示未取得产地证据，不由平台或语言推断。
        events[] 含 date、update_time、schedule、audience；同一作品可有多个播出事件。
        sources[].covered_dates 为来源实际读取日期，空列表表示未提供；partial 可为官方样本，不等于漏读日期。
        refreshing 仅表示平台采集，metadata_pending 为可选海报/映射补全，不阻挡使用已发布排期。
        保留VIP/免费事件和来源时间；未知时间不是停更，旧缓存不是刷新。本周外日期无覆盖；Bangumi周历用media.catalog。
        """
        from app.discovery.calendar.service import get_calendar_service
        from app.discovery.service import require_discovery_enabled

        require_discovery_enabled()
        if type(refresh) is not bool:
            raise ValueError("refresh 必须是布尔值")
        service = get_calendar_service()
        return service.get_week(force=refresh, wait_sources=tuple(service.providers), cancelled=_CALL_CANCELLED.get())

    @staticmethod
    @sdk_method('read')
    def resolve(provider: str, external_id: str, media_type: str, title: str, year: str = "") -> dict:
        """只读返回已确认的 TMDB 映射或完整候选；候选含真实 tmdb_id、标题、年份、media_type 和评分，不自动保存。"""
        from app.discovery.service import get_discovery_service, require_discovery_enabled

        require_discovery_enabled()
        return get_discovery_service().lookup_tmdb_mapping(
            provider, external_id, media_type, title, year,
        )

    @staticmethod
    @sdk_method('write')
    def confirm_mapping(provider: str, external_id: str, media_type: str, tmdb_id: str) -> dict:
        """按来源身份与真实 TMDB ID 预览，用户确认后保存映射。来源、目标或现有映射变化时需重新预检；不收藏、订阅或下载。"""
        raise ValueError("保存 TMDB 映射须通过工作区的 EffectExecutor 确认执行器调用")


@contextmanager
def _cloud_client():
    from app.clients.guangya import GuangYaClient, close_guangya_client

    client = GuangYaClient()
    try:
        if not client.logged_in:
            raise RuntimeError("光鸭未登录，请先在设置中登录云盘")
        yield client
    finally:
        close_guangya_client(client)


class Cloud:
    """光鸭云盘目录、文件、回收站和后台任务。"""
    @staticmethod
    @sdk_method('read')
    def organize_status(run_id: int | None = None) -> dict:
        """读取当前整理概览/排队/定时状态；传cloud.organize回执中的run_id可跨重启核对该次运行，未知不视为完成。"""
        from app.modules.organize_results import organize_status

        return organize_status(run_id)

    @staticmethod
    @sdk_method('read')
    def preview_organize(source_ids: builtins.list[str] | None = None, max_files: int = 100) -> dict:
        """只读预览已配置来源（省略source_ids选全部）；真实来源ID、目标与完整计划。max_files仅限制预览，后续整理会处理所选来源全部内容。"""
        from app.modules.organize_service import preview
        return preview(source_ids, max_files)

    @staticmethod
    @sdk_method('write')
    def organize(source_ids: builtins.list[str] | None = None) -> dict:
        """确认后按已保存规则整理所选真实来源ID；默认全部配置来源。accepted/run_id表示后台受理，完成状态来自原任务回执。"""
        from app.modules.organize_service import run
        return run(source_ids)

    @staticmethod
    @sdk_method('write')
    def stop_organize(run_id: int | None = None) -> dict:
        """确认后协作式停止当前整理，可限定真实run_id；已完成移动不回滚，stopping不代表已停止。"""
        from app.modules.organize_service import prepare_stop, stop
        preview, snapshot = prepare_stop(run_id)
        return stop(expected_snapshot=snapshot) if preview["ok"] else preview

    @staticmethod
    @sdk_method('read')
    def shares() -> dict:
        """完整读取自己创建的光鸭分享，items 保留真实 share_id（管理记录 ID）；公开链接 ID 不能用于撤销。分页失败、超预算或缺少标识会报错，空列表才表示没有分享；不返回访问码。"""
        with _cloud_client() as client:
            return {"items": client.user_share_snapshot()["items"]}

    @staticmethod
    @sdk_method('write')
    def share(file_ids: builtins.list[str], *, title: str = "", expires_days: int = 0,
              access_code: str = "", auto_access_code: bool = True,
              max_restore_count: int = 0, allow_download: bool = True) -> dict:
        """为 1–100 个真实 file_id 创建分享。工作区冻结对象、凭据及策略后确认，冷确认不重放脚本。链接和访问码只返回当前用户；accepted 仍待验证，outcome_unknown 应先查询 cloud.shares，勿重投。"""
        policy = {key: value for key, value in locals().items() if key != "file_ids"}
        with _cloud_client() as client:
            return client.submit_prepared_user_share(client.prepare_user_share(file_ids, **policy))

    @staticmethod
    @sdk_method('write')
    def revoke_share(share_ids: builtins.list[str]) -> dict:
        """撤销 cloud.shares 返回的 1–100 个真实 share_id；工作区冻结选中分享后确认。只撤销链接，不改动原文件；accepted 仍待验证，结果未知不可重投。"""
        with _cloud_client() as client:
            return client.submit_prepared_user_share(client.prepare_user_share_revoke(share_ids), revoke=True)

    @staticmethod
    @sdk_method('read')
    def organize_schedule() -> dict:
        """读取光鸭定时整理策略与环境托管标记。只读，不运行任务。"""
        from app.modules.schedule_policy import summarize_policy

        return summarize_policy("organize")

    @staticmethod
    @sdk_method('write')
    def set_organize_schedule(*, enabled: bool | EllipsisType = ..., cron: str | EllipsisType = ..., notify_enabled: bool | EllipsisType = ...) -> dict:
        """修改光鸭定时整理策略的指定字段，未传字段保留；工作区先预览确认。保存后只安排后续任务，不立即执行或中断运行。环境托管或并发修改会拒绝覆盖；runtime_refreshed=false表示配置已保存但需重启应用。"""
        changes = {name: value for name, value in locals().items() if value is not Ellipsis}
        from app.modules.schedule_policy import prepare_policy_change, apply_policy_change
        preview, fingerprint = prepare_policy_change("organize", changes)
        if preview["status"] != "confirmation_required":
            return preview
        return apply_policy_change("organize", changes, expected_fingerprint=fingerprint)

    @staticmethod
    @sdk_method('read')
    def inspect_share(url: str) -> dict:
        """完整读取光鸭分享的顶层 files（真实 id/name/is_dir/size）与当前转存目标；分页失败或到达上限会报错，不返回截断快照。只检查，不转存，令牌不公开。"""
        from app.modules.share_transfer import inspect_share_for_transfer

        return inspect_share_for_transfer(url, _owner(), "agent")

    @staticmethod
    @sdk_method('write')
    def transfer_share(url: str, file_ids: builtins.list[str]) -> dict:
        """转存分享中明确选定的真实文件ID，沿用当前配置的分享目标。工作区冻结文件与目标后确认，冷确认重新核验；原脚本不重放。回执 request_id 可用 activity.timeline('download', request_id) 跟踪整理/STRM；未知受理不可重投，转存完成不等于入库。"""
        from app.modules.share_transfer import prepare_share_transfer, submit_prepared_share_transfer

        owner = _owner()
        prepared = prepare_share_transfer(url, file_ids, owner, "agent")
        return submit_prepared_share_transfer(prepared["snapshot"], owner, user_id="agent", origin="agent", tracker_chat_id="")

    @staticmethod
    @sdk_method('read')
    def account() -> dict:
        """只读账号掩码与容量；总量、已用、可用以 byte 返回，未知为 null，部分失败保留 partial。"""
        from app.modules.guangya_account import account_status

        return account_status()

    @staticmethod
    @sdk_method('read')
    def connection() -> dict:
        """最小只读请求检查光鸭连接，返回ready/not_configured/attention/unavailable；configured、reachable为null表示未能查证，不是未配置。"""
        from app.modules.guangya_account import connection_status

        return connection_status()

    @staticmethod
    @sdk_method('read')
    def list(parent_id: str = "0", *, recursive: bool = False, query: str = "",
             kinds: builtins.list[str] | None = None, limit: int | None = None, offset: int = 0,
             max_items: int = 100_000, max_depth: int = 64, max_dirs: int = 64) -> dict:
        """按真实目录ID完整读取后过滤/分页；0为根目录。items含真实file_id/parent_id、路径及原属性，total为实际匹配总数。默认最多读取64个目录（含根），max_dirs上限5000。预算不足或翻页失败报错，不返回不完整清单；query按名称字面包含，仍会扫描全部选定范围。"""
        from app.modules.guangya_workspace import list_files

        with _cloud_client() as client:
            return list_files(client, parent_id, recursive=recursive, query=query, kinds=kinds,
                              limit=limit, offset=offset, max_items=max_items, max_depth=max_depth, max_dirs=max_dirs,
                              should_stop=_CALL_CANCELLED.get())

    @staticmethod
    @sdk_method('read')
    def preview_names(file_ids: builtins.list[str], *, mode: str = "remove_bitrate", recursive: bool = False,
                      find: str = "", replace: str = "", enrich_metadata: bool = True, max_changes: int = 10_000) -> dict:
        """真实ID名称预览；mode为remove_bitrate/replace_text/media_hygiene。媒体清理限定一个目录，保留扩展名和冲突排除。返回完整entries/conflicts/operations，无TTL编号；用cloud.change提交选定操作，按其单次容量分批。此调用不写云盘。"""
        from app.modules.guangya_rename import GuangYaNamingError, preview_rename
        from app.modules.guangya_media_hygiene import preview_media_hygiene

        if not isinstance(file_ids, builtins.list) or not file_ids or any(not isinstance(item, str) or not item or item == "0" for item in file_ids):
            raise GuangYaNamingError("必须指定真实非根文件或目录ID数组")
        with _cloud_client() as client:
            if mode == "media_hygiene":
                if len(file_ids) != 1 or find or replace:
                    raise GuangYaNamingError("媒体名称清理只接受一个目录ID，不接受文本替换")
                return preview_media_hygiene(client, file_id=file_ids[0], recursive=recursive,
                                             limit=max_changes, enrich_metadata=enrich_metadata)
            return preview_rename(client, file_ids=file_ids, mode=mode, recursive=recursive,
                                  limit=max_changes, find_text=find, replace_text=replace)

    @staticmethod
    @sdk_method('read')
    def episodes(file_id: str) -> dict:
        """按真实目录ID完整盘点剧集/字幕：items保留原文件属性、真实ID/路径，groups用真实source_path，异常和多版本不隐藏。源集号/文件数量不能推断TMDB季偏移；mapping_status=needs_verification时应核实资料或用户映射，不猜数。"""
        from app.modules.guangya_episode_naming import episode_inventory
        with _cloud_client() as client:
            return episode_inventory(client, file_id, should_stop=_CALL_CANCELLED.get())

    @staticmethod
    @sdk_method('read')
    def preview_episodes(file_id: str, *, title: str, groups: builtins.list[dict]) -> dict:
        """使用刚读取的真实source_path及明确的源季/集号范围、目标季/起点、expected_count编译operations。保持绝对集偏移与间隔，extras默认排除，未知集号不猜测；字幕仅随唯一配对视频。多版本/同名冲突需明确选择，不能覆盖。只读刷新，不存私有句柄；将完整operations交给cloud.change沿原确认/持久任务执行。"""
        from app.modules.guangya_episode_naming import preview_episode_names
        with _cloud_client() as client:
            return preview_episode_names(client, file_id, title=title, groups=groups, should_stop=_CALL_CANCELLED.get())

    @staticmethod
    @sdk_method('read')
    def preview_cleanup(source_ids: builtins.list[str] | None = None, quarantine_ids: builtins.list[str] | None = None,
                        *, include_empty: bool = True, limit: int = 100, offset: int = 0) -> dict:
        """完整扫描来源，按真实ID选择需隔离的非空残留，未选均保留；默认来源沿用整理/离线配置。candidates分页保留真实total，operations为完整选中集合：真空目录原子回收、残留移入隔离目录；从不永久删除。读取/扫描失败直接报错；cloud.change负责冻结和确认，含树快照守卫。"""
        from app.modules.guangya_residual_cleanup import preview_cleanup

        with _cloud_client() as client:
            return preview_cleanup(client, source_ids, quarantine_ids, include_empty=include_empty, limit=limit, offset=offset)

    @staticmethod
    @sdk_method('read')
    def info(file_id: str) -> dict:
        """按真实 file_id 回读光鸭文件或目录属性；item 为 null 表示不存在，不代表其他操作成功。"""
        with _cloud_client() as client:
            item = client.file_info(file_id)
            return {"item": asdict(item) if item is not None else None}

    @staticmethod
    @sdk_method('read')
    def recycle(max_items: int = 20_000) -> dict:
        """完整读取光鸭回收站并保留真实 file_id/parent_id；超过读取预算或翻页失败会报错。"""
        with _cloud_client() as client:
            return {"items": [asdict(item) for item in client.list_recycle(max_items=max_items)]}

    @staticmethod
    @sdk_method('write')
    def restore(file_ids: builtins.list[str]) -> dict:
        """恢复回收站中选定的真实file_id（最多200项）；工作区先冻结对象并确认。返回accepted/task_id仅表示受理，用cloud.task(task_id)核对终态；未知结果不要重投。"""
        from app.modules.guangya_recycle import prepare_restore, restore_prepared

        _owner()
        with _cloud_client() as client:
            return restore_prepared(client, prepare_restore(client, file_ids))

    @staticmethod
    @sdk_method('write')
    def clear_recycle() -> dict:
        """永久清空整个回收站；工作区冻结全量集合后生成高风险确认。不可恢复，Provider执行期间新进入回收站的对象也可能被删除。返回accepted/task_id后用cloud.task核对，未知结果不要重投。"""
        from app.modules.guangya_recycle import clear_prepared, prepare_clear

        _owner()
        with _cloud_client() as client:
            return clear_prepared(client, prepare_clear(client))

    @staticmethod
    @sdk_method('read')
    def offline_tasks() -> dict:
        """完整读取云盘离线任务及真实 task ID、状态、进度；离线完成不等于整理入库。"""
        with _cloud_client() as client:
            return {"items": client.list_offline_tasks()}

    @staticmethod
    @sdk_method('read')
    def task(task_id: str) -> dict:
        """读取复制、移动等云盘后台任务的原生回执；任务类型与实际状态由返回值说明。"""
        with _cloud_client() as client:
            return client.task_status(task_id)

    @staticmethod
    @sdk_method('read')
    def operation(operation_ref: str) -> dict:
        """查询 cloud.change 返回的 GY- 操作编号，跟踪同一持久任务及逐项结果；只读取当前用户的操作，受理不等于完成。"""
        from app.modules.organize_tasks import get_organize_operation_status

        return get_organize_operation_status(operation_ref, owner=_owner())

    @staticmethod
    @sdk_method('read')
    def inspect_media(file_id: str) -> dict:
        """按真实file_id检查目录或单文件；返回scope_type/scope_id、videos/companions/directories完整事实及待人工匹配原因，不执行云盘写入。"""
        from app.modules.directory_scrape import get_directory_scrape_service

        return get_directory_scrape_service().inspect_target(_owner(), file_id)


    @staticmethod
    @sdk_method('read')
    def search_media(file_id: str, query: str = "", media_type: str = "auto", year: str = "") -> dict:
        """检查真实file_id并查询匹配来源；candidates保留全部provider/external_id/tmdb_id/media_type，外层保留完整文件事实。搜索不会学习或写云盘。"""
        from app.modules.directory_scrape import get_directory_scrape_service

        return get_directory_scrape_service().search_target(_owner(), file_id, query, media_type, year)


    @staticmethod
    @sdk_method('write')
    def scrape(file_id: str, media_type: str, provider: str = "tmdb", external_id: str = "", tmdb_id: str = "",
               season: int | None = None, episode: int | None = None, numbering_mode: str = "auto") -> dict:
        """按真实file_id及明确媒体身份刮削目录/文件；tmdb使用tmdb_id或一致external_id，metatube使用external_id。season/episode/numbering_mode可人工纠正编号。工作区冻结完整移动/重命名预览后确认，返回持久operation_ref仅代表受理，用cloud.operation查询原任务；AI建议经确认不会自动学习为人工纠正。"""
        from app.modules.directory_scrape import get_directory_scrape_service

        owner = _owner()
        service = get_directory_scrape_service()
        built = service.build_execution(owner, file_id, media_type, provider=provider, external_id=external_id,
            tmdb_id=tmdb_id, season=season, episode=episode, numbering_mode=numbering_mode)
        return service.submit_execution(owner, built["execution_payload"])


    @staticmethod
    @sdk_method('write')
    def change(operations: builtins.list[dict], trigger_strm: bool = True) -> dict:
        """批量操作真实file_id：rename(new_name)、move/copy(target_path)、relocate(target_path,new_name)、trash；create_directory使用parent_path/name。trash只移入回收站；batch_files(file_id,trash_filter/target_path/cleanup_empty_dirs)按真实非根目录完整展开原批量规则，重名默认保留两份。工作区生成完整确认卡，确认后执行冻结计划并返回持久operation_ref；脚本不会重跑。"""
        from app.modules.guangya_fs_change import build_fs_change_plan, submit_fs_change_plan
        from app.repositories.organize_operation_jobs import organize_operation_public_ref

        owner = _owner()
        with _cloud_client() as client:
            plan = build_fs_change_plan(client, owner=owner, operations=operations, trigger_strm=trigger_strm)
        if plan.get("status") == "no_changes":
            return plan
        task = submit_fs_change_plan(plan["plan_id"], owner=owner, expected_fingerprint=plan["fingerprint"])
        return {**task, "operation_ref": organize_operation_public_ref(task["task_id"])}


@contextmanager
def _qb_client():
    from app.config import get_many
    from app.clients.qbittorrent import QBittorrentClient, close_qbittorrent_client

    values = get_many(("QB_URL", "QB_USERNAME", "QB_PASSWORD", "QB_API_KEY"))
    url = values["QB_URL"].strip()
    if not url:
        raise RuntimeError("qBittorrent 未配置，请先在设置中配置下载器")
    client = QBittorrentClient(
        url, username=values["QB_USERNAME"], password=values["QB_PASSWORD"],
        api_key=values["QB_API_KEY"],
    )
    try:
        yield client
    finally:
        close_qbittorrent_client(client)


class Downloads:
    """下载链接提交、qB 实时任务、MF 本地下载请求与重试。"""
    @staticmethod
    @sdk_method('write')
    def pause(hashes: list[str]) -> dict:
        """暂停指定的真实 qB hash（最多20项），工作区冻结任务后确认。accepted 仅表示受理，verification 表示真实读回；未知结果请核对 tasks，不重投。"""
        from app.modules.qb_control import prepare_qb_control, execute_qb_control

        _owner()
        return execute_qb_control(prepare_qb_control("pause", hashes))

    @staticmethod
    @sdk_method('write')
    def resume(hashes: list[str]) -> dict:
        """恢复指定的真实 qB hash（最多20项），工作区冻结后确认；本地整理写入冲突时拒绝，受理不等于状态已核验。"""
        from app.modules.qb_control import prepare_qb_control, execute_qb_control

        _owner()
        return execute_qb_control(prepare_qb_control("resume", hashes))

    @staticmethod
    @sdk_method('write')
    def remove(hashes: list[str]) -> dict:
        """移除指定的真实 qB hash（最多20项），始终保留文件；确认后先停止关联 MF 未完成下载的跟踪，远端结果未知时不要重复提交。"""
        from app.modules.qb_control import prepare_qb_control, execute_qb_control

        _owner()
        return execute_qb_control(prepare_qb_control("delete", hashes))

    @staticmethod
    @sdk_method('write')
    def submit(url: str, target: str = "preferred") -> dict:
        """确认后提交 magnet/ed2k/torrent/HTTP 下载直链；普通网页和光鸭分享不可用。

        preferred 只在预检时按当前用户偏好解析，确认冻结实际目标 qb/guangya/both。
        回执含 request_id/created/target/kind/status/duplicate/succeeded/failed；
        可用 activity.timeline("download", request_id) 跟踪。受理不代表下载或入库完成。
        已有活跃请求不会再次写后端；补投失败目标需明确调用 downloads.retry，未知受理不可重投。
        """
        from app.indexers.downloads import submit_download_url
        from app.modules.media_preferences import resolve_download_target

        owner = _owner()
        if target == "preferred":
            target, _ = resolve_download_target(owner)
        return submit_download_url(url, target, origin="agent")

    @staticmethod
    @sdk_method('read')
    def requests(scope: str = "active", limit: int = 12, offset: int = 0) -> dict:
        """分页查询 MF 本地请求，scope 为 active/attention/recent。

        items 每项含 request_number、title（已补全的文件名）、targets（目标列表）、status、
        qb_status/guangya_status、organize_status/strm_status、created_at/updated_at。
        request_number 可传 retry 或 activity.timeline("download", request_number)；
        已知提交回执 request_id 时直接查 timeline，不必遍历本列表。按 next_offset 续读。
        本查询不代表 qB 或光鸭全账户实时队列。
        """
        from app.repositories.download_requests import list_download_request_summaries

        return list_download_request_summaries(scope=scope, limit=limit, offset=offset)

    @staticmethod
    @sdk_method('read')
    def tasks(category: str = "") -> dict:
        """完整读取 qB 任务列表；保留真实 hash、原生 state/progress，读取失败不会返回空列表。"""
        with _qb_client() as client:
            return {"items": [asdict(item) for item in client.list_torrents(category=category)]}

    @staticmethod
    @sdk_method('read')
    def transfer() -> dict:
        """读取 qB 连接状态、传输速率和累计量；下载器状态不代表整理或入库完成。"""
        with _qb_client() as client:
            return asdict(client.get_transfer_info())

    @staticmethod
    @sdk_method('read')
    def diagnose() -> dict:
        """只读诊断 qBittorrent 队列、传输连接和疑似停滞；返回 ok/status/data。

        data 保留 connection/summary/attention_tasks/attention_truncated，关注任务含真实 hash。
        queue_state=reachable 且 summary.total=0 才是已读空队列；partial 保留成功事实，
        unavailable_sources 标明未读范围，unavailable 不代表空队列。快照不能证明持续停滞、
        整理或入库完成；不公开凭据、路径或下载链接。完整任务用 tasks() 读取。
        """
        from app.modules.download_diagnosis import diagnose_download_queue

        return diagnose_download_queue()

    @staticmethod
    @sdk_method('read')
    def version() -> dict:
        """读取 qB 应用和 Web API 版本；鉴权或请求失败直接报错。"""
        with _qb_client() as client:
            if not client.api_key and not client.login():
                raise RuntimeError("qBittorrent 鉴权失败")
            return client.get_version()

    @staticmethod
    @sdk_method('read')
    def files(torrent_hash: str) -> dict:
        """按真实 qB hash 完整读取任务文件；返回 index/name/size/progress，不截取前50项。"""
        import re

        if not isinstance(torrent_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", torrent_hash.strip()):
            raise ValueError("torrent_hash 必须是40或64位真实任务hash")
        with _qb_client() as client:
            return {"items": [asdict(item) for item in client.get_torrent_files(torrent_hash.strip().lower())]}

    @staticmethod
    @sdk_method('write')
    def retry(request_id: int, target: str) -> dict:
        """确认后重试明确的 MF 下载请求，target 必须是 qb/guangya/both。只补可重试目标，未知受理不重投；预检冻结原请求与配置。回执包含 source_request_id 和实际 request_id，可用 activity.timeline 查询后续阶段。受理不代表下载或入库完成。"""
        from app.modules.download_dispatcher import (
            prepare_download_retry, public_download_retry_result, resubmit_download_request,
        )

        snapshot = prepare_download_retry(request_id, target)
        result = resubmit_download_request(request_id, snapshot["target"], origin="agent",
                                           expected_fingerprint=snapshot["fingerprint"])
        return public_download_retry_result(result, source_request_id=request_id, target=snapshot["target"])


class Strm:
    """STRM同步、失败账本与选择性重试、持久运行状态及伴随元数据控制。"""
    @staticmethod
    @sdk_method('read')
    def failures(status: str = "all", action: str = "", source_id: str = "", limit: int = 200, offset: int = 0) -> dict:
        """只读失败账本，data.items保留真实id/status/action/来源/对象/文件/路径及完整已保存error。error已在入库时脱敏且最多500字，不是可还原的上游原文；无额外摘要截断。status为all/open/retrying/resolved，action为空/generate/metadata；limit为1-1000，按data.next_offset读完。data.failures是全账本聚合，不能替代明细。"""
        from app.modules.strm import strm_failures

        return strm_failures(status, action, source_id, limit, offset)

    @staticmethod
    @sdk_method('write')
    def retry_failures(ids: builtins.list[int]) -> dict:
        """确认后只重试明确的真实失败ID数组，取自failures().data.items中open记录；无scope或100项模型上限。预检冻结选中行及运行配置，变化时需重新预检。回执data保留requested/matched/resolved/failed/missing/stale/deferred及扫描/停止事实；matched小于requested表示并发状态变化，不能宣称全部完成。"""
        from app.modules.strm import prepare_strm_failure_retry, retry_selected_strm_failures

        prepared = prepare_strm_failure_retry(ids)
        if prepared["status"] != "confirmation_required":
            return {key: value for key, value in prepared.items() if key != "fingerprint"}
        return retry_selected_strm_failures(ids, expected_fingerprint=prepared["fingerprint"], should_stop=_CALL_CANCELLED.get())

    @staticmethod
    @sdk_method('read')
    def schedule() -> dict:
        """读取STRM定时同步策略与环境托管标记。只读，不运行任务。"""
        from app.modules.schedule_policy import summarize_policy

        return summarize_policy("strm")

    @staticmethod
    @sdk_method('write')
    def set_schedule(*, enabled: bool | EllipsisType = ..., cron: str | EllipsisType = ..., notify_enabled: bool | EllipsisType = ...) -> dict:
        """修改STRM定时同步策略的指定字段，未传字段保留；工作区先预览确认。保存后只安排后续任务，不立即执行或中断运行。环境托管或并发修改会拒绝覆盖；runtime_refreshed=false表示配置已保存但需重启应用。"""
        changes = {name: value for name, value in locals().items() if value is not Ellipsis}
        from app.modules.schedule_policy import prepare_policy_change, apply_policy_change
        preview, fingerprint = prepare_policy_change("strm", changes)
        if preview["status"] != "confirmation_required":
            return preview
        return apply_policy_change("strm", changes, expected_fingerprint=fingerprint)

    @staticmethod
    @sdk_method('read')
    def history(status: str = "all", limit: int = 50, offset: int = 0) -> dict:
        """读取完整STRM持久历史分页；status按账本状态all/running/success/partial/failed/skipped过滤。返回items(真实run_id/统一status/原raw_status/安全统计)、total/returned/has_more/next_offset及failure_context/queue_context；50只是默认页长。逐页用next_offset继续；错误会报错，未知记录保留，不把页尾当没有历史。"""
        from app.modules.scheduler import strm_history

        return strm_history(status=status, limit=limit, offset=offset)

    @staticmethod
    @sdk_method('write')
    def run(source_ids: builtins.list[str] | None = None) -> dict:
        """确认后手动同步全部或指定真实STRM来源ID，按既有规则创建/更新/清理文件及刷新媒体库。来源ID取status().data.sources[].id；返回ok/status/data.run_id，accepted仅表示受理，随后用status(run_id)核验。"""
        from app.modules.scheduler import prepare_strm_run, submit_strm_run

        prepared = prepare_strm_run(source_ids)
        return submit_strm_run(source_ids, expected_fingerprint=prepared["fingerprint"])

    @staticmethod
    @sdk_method('read')
    def status(run_id: int | None = None) -> dict:
        """读取STRM状态；无run_id返回data.sources(真实id/name)、当前进度与last_run；指定正整数返回该原始任务data.task(status/stats/progress)。进度只代表当前进程且仅在run_id匹配时返回；未知记录为unknown，不回退最近任务。"""
        from app.modules.scheduler import strm_status

        return strm_status(run_id)

    @staticmethod
    @sdk_method('read')
    def diagnose() -> dict:
        """只读检查STRM索引文件存在性和失败汇总，不修复、不重试、不扫描云盘；返回data.index/failures/recent_runs，最近5条保留真实run_id并明确recent_limit。"""
        from app.modules.scheduler import diagnose_strm

        return diagnose_strm()

    @staticmethod
    @sdk_method('read')
    def metadata_status() -> dict:
        """读取伴随元数据队列的完整业务状态；worker_scope=current_process，线程状态仅代表本进程。"""
        from app.modules.strm_metadata_management import metadata_status

        return metadata_status()


    @staticmethod
    @sdk_method('write')
    def set_metadata_enabled(enabled: bool) -> dict:
        """调整STRM伴随元数据同步开关；关闭不清空积压、不终止running，已运行扫描可能继续入队。工作区需确认；返回enabled/verified/summary，verified=false表示写后运行态未核验一致，勿盲目重投。"""
        from app.modules import strm_metadata_management as service

        owner = _owner()
        _, token = service.prepare_policy(enabled, owner)
        return service.set_policy_confirmed(token, owner)

    @staticmethod
    @sdk_method('write')
    def cancel_metadata_pending() -> dict:
        """确认取消预览时queued/retry_wait积压，不改变开关、不杀running、不删除文件；后来新增任务保留。返回cancelled/enabled/files_deleted/summary。"""
        from app.modules import strm_metadata_management as service

        owner = _owner()
        _, token = service.prepare_backlog_cancel(owner)
        return service.cancel_backlog_confirmed(token, owner)


class Playback:
    """媒体反代实例管理与播放诊断；真实instance_id来自instances，凭据保持脱敏。"""
    @staticmethod
    @sdk_method('read')
    def instances() -> dict:
        """完整读取已保存实例、有效配置、错误与本进程运行事实，不启动监听或探测上游。data.instances[].instance_id是真实ID，和列表位置无关；runtime_status=unknown表示运行态未能读取。"""
        from app.modules.media_proxy import playback_proxy_instances

        return playback_proxy_instances()

    @staticmethod
    @sdk_method('read')
    def failures(hours: int = 24, instance_id: int | None = None, limit: int = 50, offset: int = 0) -> dict:
        """读取最近1/6/24/72小时的原故障汇总及完整已保存失败记录。data.items保留真实记录/实例ID、状态、阶段、路由、时延和error；error已在入库脱敏并最多1000字，不是上游原文。limit为1-200，按data.next_offset读完；summary_group_limit=8只限制汇总排名，明细可完整分页。coverage仅代表best-effort已记录请求，不代表媒体服务器全部播放。"""
        from app.modules.media_proxy import playback_proxy_failures

        return playback_proxy_failures(hours, instance_id, limit, offset)

    @staticmethod
    @sdk_method('read')
    def test(instance_id: int) -> dict:
        """对真实instance_id执行一次既有固定上游健康探测，不跟随重定向，返回HTTP状态、耗时与失败类型；不修改或重启实例。"""
        from app.modules.media_proxy import test_playback_proxy

        return test_playback_proxy(instance_id)

    @staticmethod
    @sdk_method('write')
    def set_enabled(instance_id: int, enabled: bool) -> dict:
        """确认后启停明确的真实实例ID；冻结该实例与有效配置，漂移需重新预检。只更新enabled，清理缓存并请求既有运行时协调；runtime_refreshed只表示排队，未排队时配置仍已保存。"""
        from app.modules.media_proxy import apply_playback_proxy_change, prepare_playback_proxy_change

        prepared = prepare_playback_proxy_change("set_enabled", instance_id, enabled)
        return apply_playback_proxy_change("set_enabled", instance_id, enabled,
            expected_fingerprint=prepared["fingerprint"], cancelled=_CALL_CANCELLED.get())

    @staticmethod
    @sdk_method('write')
    def restart(instance_id: int) -> dict:
        """确认后仅重建该真实ID的已启用反代实例，沿原Manager和Future，不改配置。冻结目标/有效配置，可能中断播放；completed须有真实restarted=true，outcome_unknown时先读instances核实，勿重复提交。"""
        from app.modules.media_proxy import apply_playback_proxy_change, prepare_playback_proxy_change

        prepared = prepare_playback_proxy_change("restart", instance_id)
        return apply_playback_proxy_change("restart", instance_id,
            expected_fingerprint=prepared["fingerprint"], cancelled=_CALL_CANCELLED.get())
class Local:
    """本地来源、持久任务与扫描；source_id/task_id取读取结果的真实id，无会话序号。"""
    @staticmethod
    @sdk_method('read')
    def sources() -> dict:
        """完整读取本地来源和targets，保留真实id、名称、容器目录、qB前缀/接管开关及归档/媒体库映射；不扫描文件，不返回废弃SMB凭据。"""
        from app.modules.local_media_service import local_sources
        items = local_sources()
        return {"items": items, "total": len(items)}

    @staticmethod
    @sdk_method('read')
    def source(source_id: int) -> dict:
        """按真实来源ID读取完整来源/targets；不存在会报错，不回退到列表位置。"""
        from app.modules.local_media_service import local_source
        return local_source(source_id)

    @staticmethod
    @sdk_method('write')
    def create_source(name: str, local_root: str, *, qb_profile: str = "configured:qb", qb_path_prefix: str = "", enabled: bool = False,
                      media_type: str = "auto", mode: str = "preview_only", targets: builtins.list[dict] | None = None) -> dict:
        """确认后创建来源；容器目录必须已存在，不改权限或媒体文件。默认preview_only且关闭qB完成接管；targets沿Web分类/路径校验，未提供则需后续配置。"""
        changes = dict(locals())
        from app.modules.local_media_service import apply_local_source_change, prepare_local_source_change
        prepared = prepare_local_source_change("create", changes=changes)
        return apply_local_source_change("create", changes=changes, expected_fingerprint=prepared["fingerprint"], cancelled=_CALL_CANCELLED.get())

    @staticmethod
    @sdk_method('write')
    def update_source(source_id: int, *, name: str | EllipsisType = ..., local_root: str | EllipsisType = ...,
                      qb_profile: str | EllipsisType = ..., qb_path_prefix: str | EllipsisType = ..., enabled: bool | EllipsisType = ...,
                      media_type: str | EllipsisType = ..., mode: str | EllipsisType = ..., targets: builtins.list[dict] | None | EllipsisType = ...) -> dict:
        """确认后更新指定字段；未提供的字段和targets原样保留。enabled控制qB完成自动接管，无额外trigger入口。有未完成任务时沿Web事务禁止修改运行配置；重校验来源及映射，漂移需新预检。"""
        changes = {key: value for key, value in locals().items() if key != "source_id" and value is not Ellipsis}
        from app.modules.local_media_service import apply_local_source_change, prepare_local_source_change
        prepared = prepare_local_source_change("update", source_id, changes)
        return apply_local_source_change("update", source_id, changes, expected_fingerprint=prepared["fingerprint"], cancelled=_CALL_CANCELLED.get())

    @staticmethod
    @sdk_method('write')
    def delete_source(source_id: int) -> dict:
        """确认后只删除真实ID的来源配置，不删除媒体文件或修改权限；活动任务阻止删除。来源/targets漂移、取消或票据已用时不重放。"""
        from app.modules.local_media_service import apply_local_source_change, prepare_local_source_change
        prepared = prepare_local_source_change("delete", source_id)
        return apply_local_source_change("delete", source_id, expected_fingerprint=prepared["fingerprint"], cancelled=_CALL_CANCELLED.get())

    @staticmethod
    @sdk_method('write')
    def scan_sources(source_ids: builtins.list[int] | None = None, query: str = "") -> dict:
        """确认后扫描全部或所选真实来源ID并入原持久任务队列；预检冻结真实ID/来源/targets，未配置目标或仅预览来源跳过。query仅过滤媒体标题。data.task_ids是实际持久任务ID，scan_ref沿原task_runs；扫描失败不表示无候选，accepted不代表媒体归档完成。"""
        from app.modules.local_media_service import prepare_local_source_scan, scan_local_sources
        prepared = prepare_local_source_scan(source_ids, query)
        return scan_local_sources(**prepared["arguments"], expected_fingerprint=prepared["fingerprint"], cancelled=_CALL_CANCELLED.get())


    @staticmethod
    @sdk_method('read')
    def tasks(scope: str = "all", *, source_id: int | None = None, scan_ref: str = "", limit: int = 100, offset: int = 0) -> dict:
        """按真实持久任务ID读取完整任务/错误和文件事实；total为匹配总数，returned为本页条数，按has_more/next_offset穷尽。limit 1–500。scope支持all/attention/active/history/skipped/latest_scan和持久状态。scan_ref限定原LM批次，缺失不以历史替代；scan_outcome保留当次通知事实，current_status表示现在。"""
        from app.modules.local_media_tasks import local_tasks
        return local_tasks(scope, source_id=source_id, scan_ref=scan_ref, limit=limit, offset=offset)

    @staticmethod
    @sdk_method('read')
    def task(task_id: int, *, inspect: bool = False) -> dict:
        """读取真实ID的完整任务、全部文件项及执行步骤，完整错误仅脱敏凭据。inspect=True沿Web只读检查待人工任务的全部文件，不创建序号/TTL上下文；不移动文件。"""
        from app.modules.local_media_tasks import local_task
        return local_task(task_id, inspect=inspect)

    @staticmethod
    @sdk_method('read')
    def preview(task_id: int, *, tmdb_id: str | EllipsisType = ..., media_type: str | EllipsisType = ...,
                season: int | None | EllipsisType = ..., episode: int | None | EllipsisType = ...,
                numbering_mode: str | EllipsisType = ...) -> dict:
        """按持久任务重新只读检查并生成完整整理预览；可明确选择TMDB身份、媒体类型/季集/编号模式，未提供沿保存值。沿原service路径/规则校验，不接受任意路径或检查句柄；返回所有候选/计划及原业务状态，不执行文件操作。"""
        changes = {key: value for key, value in locals().items() if key != "task_id" and value is not Ellipsis}
        from app.modules.local_media_tasks import preview_local_task
        return preview_local_task(task_id, **changes)

    @staticmethod
    @sdk_method('write')
    def retry(task_id: int, *, tmdb_id: str | EllipsisType = ..., media_type: str | EllipsisType = ...,
              season: int | None | EllipsisType = ..., episode: int | None | EllipsisType = ...,
              numbering_mode: str | EllipsisType = ..., confirm_interrupted_write: bool = False) -> dict:
        """确认后仅重新排队原failed/requires_manual持久任务。可明确覆盖Web支持的识别身份、季集/编号，省略则保留；确认冻结任务/来源/targets/必要文件预览，原SQLite事务CAS。不扫描新范围、不直接移动文件。中断写入必须先人工核验文件及qB，再显式confirm_interrupted_write=True；取消/漂移/未知回执不重放。"""
        changes = {key: value for key, value in locals().items() if key != "task_id" and value is not Ellipsis}
        from app.modules.local_media_tasks import prepare_local_task_retry, retry_local_task
        prepared = prepare_local_task_retry(task_id, **changes)
        return retry_local_task(task_id, **changes, expected_fingerprint=prepared["fingerprint"], cancelled=_CALL_CANCELLED.get())

    @staticmethod
    @sdk_method('write')
    def refresh_library(task_id: int) -> dict:
        """确认后按已完成任务、全部归档路径和重新解析的唯一媒体库绑定提交精准刷新；冻结真实task_id/配置/目标，不退化全库。共用原刷新客户端和跨owner去重租约；未知回执先verify_library核验，勿直接重投。"""
        from app.modules.local_media_library import prepare_local_task_refresh, refresh_local_task_library
        prepared = prepare_local_task_refresh(task_id)
        return refresh_local_task_library(task_id, expected_fingerprint=prepared["fingerprint"], cancelled=_CALL_CANCELLED.get())

    @staticmethod
    @sdk_method('read')
    def verify_library(task_id: int) -> dict:
        """只读核验已完成任务的精确电影/季集在唯一绑定媒体库中的索引可见性；错误或不完整清单保持不确定，不等同于不存在；不发起播放探测。"""
        from app.modules.local_media_library import verify_local_task_library
        return verify_local_task_library(task_id)

    @staticmethod
    @sdk_method('read')
    def diagnose() -> dict:
        """读取本地来源/持久任务真实聚合及已存在调度器状态；不创建调度器、不扫描目录、不联网。读取错误直接反馈，不能以零计数宣称没有问题。"""
        from app.modules.local_media_tasks import diagnose_local_tasks
        return diagnose_local_tasks()


class Web:
    """公网网页搜索与正文读取。"""
    @staticmethod
    @sdk_method('read')
    def search(
        query: str, max_results: int = 5, topic: str = "general", time_range: str | None = None, *,
        include_domains: list[str] | None = None, exclude_domains: list[str] | None = None,
        start_date: str | None = None, end_date: str | None = None,
    ) -> dict:
        """Tavily 公网搜索，data.results 含 title/url/snippet/published_date（若来源提供）。

        include_domains/exclude_domains 为裸域名列表，如 ["jellyfin.org"]，包含其子域名。
        start_date/end_date 为 YYYY-MM-DD，可单独使用；不能与 time_range 同用。
        日期筛选依据供应商的发布/更新时间；具体发布日期需读取原文核实。
        max_results 为1–10，仍受设置中的上限约束；data.max_results/filters 返回实际范围。
        摘要不代表已读正文；用 read 打开来源后引用真实 URL。
        """
        from app.modules.web_evidence import search_web

        return search_web(
            query, max_results=max_results, topic=topic, time_range=time_range,
            include_domains=include_domains, exclude_domains=exclude_domains,
            start_date=start_date, end_date=end_date,
        )

    @staticmethod
    @sdk_method('read')
    def read(url: str, max_chars: int = 2_000_000) -> dict:
        """本地读取公开 HTTPS 的 HTML、文本或 JSON（响应上限2MiB）；data.content 保留预算内正文，truncated 说明是否读全。

        data.url 为重定向后的来源；HTML 正文保留首次引用的目标地址，links=[{text,url}] 可继续 read，已转绝对地址并去重。
        公开 API 的 JSON 保留结构和值（敏感值仍脱敏），未截断时可 json.loads(data["content"]) 后按字段处理。
        links 保留本次响应中全页正文的可读链接，links_total 为去重后数量；不受正文 max_chars 截断影响。
        不执行 JavaScript，不读取登录页、验证码或 PDF；不可读时可搜索同一事实的其他公开来源。
        """
        from app.modules.web_evidence import read_web

        return read_web(url, max_chars=max_chars)


class Activity:
    """操作历史、整理日志、可撤销操作检查与补偿，以及下载和入库活动跟踪。"""
    @staticmethod
    @sdk_method('read')
    def actions(limit: int = 50, outcome: str = "all", before_id: int | None = None) -> dict:
        """当前身份经确认执行的审计记录，真实ID倒序分页。outcome为all/success/failed，pending/unknown不会冒充失败或成功。count仅当前页，has_more时用next续取；沿已保存的脱敏事实，不含原始参数或票据。"""
        from app.agent.action_history import read_actions

        return read_actions(owner=_owner(), limit=limit, outcome=outcome, before_id=before_id)

    @staticmethod
    @sdk_method('read')
    def search(query: str = "", limit: int = 50, offset: int = 0) -> dict:
        """分页查询活动，返回真实总数、kind/id及next_offset；完成某一阶段不等于整个流程完成。"""
        from app.repositories.activity import search_page

        return search_page(query=query, limit=limit, offset=offset)

    @staticmethod
    @sdk_method('read')
    def organize_logs(origin: str = "all", status: str = "all", keyword: str = "",
                      limit: int = 50, offset: int = 0) -> dict:
        """分页查询整理日志，origin为all/guangya/local；status为all/failed/manual/processing/success/skipped/reverted/deleted/issues。与Web日志同一筛选口径，返回items、total、by_origin/by_status、has_more/next_offset；记录保留真实id/kind、原始及规范状态、完整标题/路径和脱敏错误。kind/id可传activity.timeline，回收成功为deleted。读取失败抛出异常，不代表没有记录。 本地待确认使用origin="local",status="manual"；终态用success/failed，raw_status保留completed/failed，trigger和时间戳可自行统计。"""
        from app.repositories.activity import organize_logs
        return organize_logs(origin=origin, status=status, keyword=keyword, limit=limit, offset=offset)

    @staticmethod
    @sdk_method('read')
    def timeline(kind: str, identifier: int) -> dict:
        """按持久ID核对各阶段；kind为download/organize/local_media。

        downloads.submit 回执 request_id、resources.download 回执 data.items[].request_id
        直接传 timeline("download", request_id)，无需搜索或匹配云盘列表。
        返回 ok/status/message/data；data.title 是已补全的文件名，data.request 是下载请求摘要，
        data.stages 是阶段列表（stage/status/task_bound/progress/updated_at/reason），不是 events。
        data.organize_evidence 提供本请求绑定来源的媒体身份及文件ID；它是持久整理回执，不是实时云盘/媒体库查询。
        下载阶段 task_bound=True 表示该请求已绑定后端任务；False 表示尚未记录关联，不证明后端没有任务。
        data.gaps 标明未复核范围，freshness=persisted_snapshot；下载完成不等于媒体库已入库。
        """
        from app.repositories.activity import timeline_snapshot

        return timeline_snapshot(kind, identifier)

    @staticmethod
    @sdk_method('read')
    def organize_detail(log_id: int) -> dict:
        """读取单条光鸭整理日志的媒体身份、成员 file_id、版本及 allowed_actions。

        log_id 来自 organize_logs。纠正季集后入库使用 reorganize，无需先改文件或父目录名称。
        """
        from app.modules.organize_correction import OrganizeCorrectionService

        service = OrganizeCorrectionService()
        try:
            return service.detail(log_id)
        finally:
            service.close()

    @staticmethod
    @sdk_method('write')
    def reorganize(log_id: int, tmdb_id: str, media_type: str, season: int | None = None,
                   episode: int | None = None, provider: str = "tmdb", external_id: str = "") -> dict:
        """按日志纠正身份/季集并整理入库，使用与 Web 日志相同的预览、执行和收尾。

        例：activity.reorganize(log_id, tmdb_id="276288", media_type="tv", season=2, episode=6)。
        季集取用户明确指定值或核实后的资料；工作区生成一次完整变更确认，随后移动、改名、
        更新原日志和待确认状态，并按配置联动 STRM。accepted 只表示受理，宿主跟踪本次任务；
        activity.timeline("organize", log_id) 可核对该条日志的归档状态和操作记录。
        AI 选择保留 agent 来源，不自动沉淀为人工识别知识。
        """
        raise ValueError("日志重新整理须通过工作区 EffectExecutor 确认执行")


    @staticmethod
    @sdk_method('read')
    def inspect_undo(log_id: int) -> dict:
        """只读核对真实整理日志 log_id 的最近可逆移动/改名，编号来自 activity.search 的 organize 项 id。
        返回 data.receipt_id 补偿凭据、affected 总数和 changes 撤销预览（最多20项）。
        changes[].from 是当前名称，to 是撤回后的名称；这是将要回退的方向，不是历史操作方向。
        下载提交、本地整理和永久删除不能猜测撤销；此方法不执行云盘写入。
        """
        from app.modules.activity_undo import inspect_undo

        return inspect_undo(log_id, owner=_owner())

    @staticmethod
    @sdk_method('write')
    def undo(receipt_id: str) -> dict:
        """使用宿主返回的真实 receipt_id 回退媒体偏好或整理日志。私有快照只从补偿账本读取，不接受自造载荷或旧 action/selection ref。工作区冻结计划并结束脚本，确认后复验 owner、一天时效和当前版本，仅消费一次。明确未受理可重新预检；已受理、执行失败或写后未知均不可重投，按原 log_id 查询时间线。无私有快照的历史回执明确失效；整理需重新 inspect。不会撤回下载或通知。"""
        from app.modules.activity_undo import execute_undo, prepare_undo

        owner = _owner()
        prepared = prepare_undo(receipt_id, owner=owner)
        return execute_undo(receipt_id, expected_fingerprint=prepared["fingerprint"], owner=owner)

    @staticmethod
    @sdk_method('write')
    def follow(kind: str, identifier: int, hours: int = 24) -> dict:
        """确认后关注持久活动kind/id，已有跟踪按本次小时数续期；异常、阶段结束或到期通知一次。只观察，不重试或取消原任务；完成下载不等于入库。"""
        from app.repositories import activity as repository

        owner = _owner()
        prepared = repository.prepare_follow(kind, identifier, owner=owner, hours=hours)
        return repository.follow(kind, identifier, owner=owner, hours=hours, expected_snapshot=prepared["snapshot"])

    @staticmethod
    @sdk_method('read')
    def follows() -> dict:
        """列出当前用户持久保存的活动跟踪规则及启用、到期状态。"""
        from app.repositories.activity import follows

        return follows(owner=_owner())

    @staticmethod
    @sdk_method('write')
    def unfollow(rule_id: str) -> dict:
        """确认后停用当前用户的跟踪规则，不停止原任务；已经停用时无需变更。"""
        from app.repositories import activity as repository

        owner = _owner()
        prepared = repository.prepare_unfollow(rule_id, owner=owner)
        if prepared["preview"]["status"] == "no_changes":
            return prepared["preview"]
        return repository.unfollow(rule_id, owner=owner, expected_snapshot=prepared["snapshot"])


class Resources:
    """公开资源站的资源搜索、版本筛选和季集匹配。"""
    @staticmethod
    @sdk_method('control')
    def present(result_ids: list[str], *, resolution: str | EllipsisType = ...,
                dynamic_range: str | EllipsisType = ..., exclude_hdr: bool | EllipsisType = ...) -> dict:
        """展示当前会话读到的候选，支持跨作品、查询和分页选择0–50个唯一 result_id；[] 撤卡。
        此方法只更新Web/TG候选卡；下载预览用 resources.download，取消确认用 session.pending_plan("cancel")。

        省略规格沿用各项原查询，显式规格继续筛选；放宽条件需重新搜索。
        有季集条件时只展示匹配单集/范围包；rank_episode 重排结果也可展示。
        返回实际 result_ids、queries（各查询条件与来源覆盖）、coverage_notice、excluded_count；
        单查询另有 effective_query。scope.complete 表示选全会话已观察ID，来源覆盖另行报告。
        search_missing 的 verification_targets 随选中ID保留；原始候选仍可从查询结果研究。

            selected = r["recommendation"]["selected"]
            if selected:
                cards = resources.present([selected["result_id"]])
        """
        from app.modules.resource_candidates import select_candidates

        return select_candidates(_RESOURCE_CANDIDATES.get(), result_ids,
            {key: value for key, value in {"resolution": resolution, "dynamic_range": dynamic_range,
                                          "exclude_hdr": exclude_hdr}.items() if value is not Ellipsis})


    @staticmethod
    @sdk_method('read')
    def sources() -> dict:
        """读取资源搜索总开关、各站启用状态、下载类型及分页能力。

        返回status、counts、sites和configuration手动站点配置；scope=agent与search使用同一站点权限。
        只读取本地配置与注册表，不探测网络；ready不代表站点当前可访问。
        disabled/no_enabled_sites/unavailable分别表示关闭、无可用站点、读取失败。
        """
        from app.indexers.runtime import source_readiness

        return source_readiness(scope="agent")

    @staticmethod
    @sdk_method('write')
    def configure_sites(site_ids: list[str], enable_search: bool | None = None) -> dict:
        """确认后保存手动站点选择并刷新Web/TG检索服务；省略enable_search保留总开关。

        使用sources().configuration读取当前选择和部署管理状态。
        site_ids为手动可选站点ID非空列表（不含自动附加来源）；启用Sukebei会明确列入确认计划。
        """
        raise ValueError("资源站点修改须通过工作区EffectExecutor确认执行")

    @staticmethod
    @sdk_method('read')
    def search(
        title: str, original_title: str = "", english_title: str = "",
        aliases: list[str] | None = None, year: int | None = None,
        media_type: str = "", season: int | None = None, episode: int | None = None,
        page: int = 1, sites: list[str] | None = None, sort_mode: str = "relevance_desc",
        resolution: str = "", dynamic_range: str = "", exclude_hdr: bool = False,
    ) -> dict:
        """多站搜索当前页资源；传 season/episode 时同时核对季集，调用保持只读。

        返回 items（result_id/title/site_name/size_bytes/seeders）、request（可复用的完整查询条件）、
        site_statuses（各站状态/next_page）、partial/has_more/complete（覆盖）、technical_filter（画质依据）。
        体积/做种数为 None 表示未知。items 是全部候选，展示卡片使用 present；新搜索撤下旧卡，
        presentation.result_ids 反映当前卡片。指定季集时 items[].quality 给出 match/eligible/tags；
        recommendation 含 target/selected/alternatives/status，selected 可为 None。
        recommendation.matched_count 为 matched_counts 中 exact_episode 单集与 episode_pack 范围包之和，仅计当前范围。

        resolution: 2160p/1080p/720p/480p；dynamic_range: SDR/HDR/HDR10/HDR10+/DoVi/HLG，空串不限。
        exclude_hdr 仅排除已标 HDR；明确要求 SDR 用 dynamic_range="SDR"。标题判据不等于实测文件质量。

            r = resources.search("目标剧", season=1, episode=2, resolution="1080p")
            print(r["recommendation"])
            next_season = resources.search(**{**r["request"], "season": 2})
        """
        from app.indexers.models import IndexerMediaSearchRequest
        from app.indexers.ranking import filter_technical_candidates, technical_filter_arguments
        from app.indexers.runtime import get_indexer_service, run_indexer_awaitable_sync
        from app.indexers.service import search_payload

        quality = technical_filter_arguments({
            "resolution": resolution, "dynamic_range": dynamic_range, "exclude_hdr": exclude_hdr,
        })
        request = IndexerMediaSearchRequest.create(
            title=title, original_title=original_title, english_title=english_title,
            aliases=aliases, year=year, media_type=media_type, season=season,
            episode=episode, page=page, sort_mode=sort_mode,
        )
        service = get_indexer_service()
        result = run_indexer_awaitable_sync(service.search_media(request, site_ids=sites, scope="agent"),
                                          cancelled=_CALL_CANCELLED.get())
        payload = search_payload(service, result)
        payload["items"], payload["technical_filter"] = filter_technical_candidates(payload["items"], quality)
        search = {"request": {**asdict(request), "sites": list(sites) if sites is not None else None, **quality}, **payload}
        if request.season is not None and request.episode is not None:
            return Resources.rank_episode(search, request.season, request.episode)
        return search

    @staticmethod
    @sdk_method('read')
    def search_missing(items: list[dict], as_of: str | None = None, sites: list[str] | None = None,
                       page: int = 1, max_episodes: int | None = None, limit_per_episode: int = 8,
                       preference_overrides: dict | None = None) -> dict:
        """批量核对已播缺集并搜索资源，保持只读。items 为1–12项，每项必填 query/season，
        可选 tmdb_id/library_name/episodes。默认每部20集；指定 episodes 最多100集，max_episodes 可缩小。
        默认采用用户偏好，preference_overrides 可覆盖；page/sites 选择来源页。
        单季最多6次、整批24次搜索，共享45秒；limit_per_episode 仅限制每集推荐ID数量。

        返回：
        - items：按 result_id 去重的原始候选全文。
        - groups[]：query/season/status/audit、remaining_episodes/failed_episodes/stop_reason。
        - groups[].episodes[]：season/episode/recommended_result_ids/search。
          search.recommendation.selected 为首选或 None，source_url 可为空；search.result_ids 引用根 items，
          按逐集排名排列。quality/preference_match 以 result_id 为键保存判据，未设偏好时后者为空。
        - groups[].searches[]：实际请求页，以 result_ids 引用根 items，保留 site_statuses/next_page。
          两处 search 都保留 request、状态、错误和覆盖，不重复嵌入候选全文。
        - result_ids/verification_targets：首选ID及完整季集关联，传递给展示或下载时一起保留。
        partial/has_more/complete 是覆盖；失败或剩余目标是未完成范围，不等于没有资源。

            r = resources.search_missing([{"query": "目标剧", "season": 1}])
            for group in r["groups"]:
                print(group["query"], group["remaining_episodes"], group["failed_episodes"])
                for episode in group["episodes"]:
                    print(episode["season"], episode["episode"], episode["search"]["recommendation"]["selected"])
        """
        from app.modules.missing_resources import search_missing

        return search_missing(items, owner=_owner(), as_of=as_of, sites=sites, page=page,
                              max_episodes=max_episodes, limit_per_episode=limit_per_episode,
                              preference_overrides=preference_overrides, cancelled=_CALL_CANCELLED.get() or (lambda: False),
                              checkpoint=_READ_CHECKPOINT.get())

    @staticmethod
    @sdk_method('read')
    def missing_workflows(limit: int = 10, offset: int = 0) -> dict:
        """读取当前用户持久补库流程的真实workflow_id/item_id/revision及下载request_id和状态。保留所选流程的全部成员；total为当前用户流程总数，has_more/next_offset供分页。不存在为empty，数据库/身份错误不转空。limit范围1至30。"""
        from app.modules.missing_resources import missing_workflows

        return missing_workflows(owner=_owner(), limit=limit, offset=offset)

    @staticmethod
    @sdk_method('read')
    def rank_episode(
        search: dict, season: int, episode: int, audit: dict | None = None,
        preferences: dict | None = None,
    ) -> dict:
        """用已有 search 返回值核对指定季集，可传 library.audit 的完整 audit 补充映射；不请求资源站。
        返回新字典，保留原 request/items/site_statuses/覆盖，更新 items[].quality 和 recommendation。
        recommendation.target 是本次季集，selected/alternatives 是建议；matched_count 为 eligible 单集与
        范围包之和，matched_counts 分列两者，candidate_count 还含编号待核对项。标题匹配不等于文件已验证。
        preferences 是软排序偏好（preferred_resolution/preferred_codecs/preferred_subtitles），保留 search 硬过滤。

            ranked = resources.rank_episode(r, season=1, episode=3, audit=audit)
            print(ranked["recommendation"])
        """
        from app.indexers.models import IndexerMediaSearchRequest
        from app.modules.episode_resource_ranking import rank_episode_search

        arguments = dict(search["request"])
        for key in ("sites", "resolution", "dynamic_range", "exclude_hdr"):
            arguments.pop(key, None)
        arguments.update(season=season, episode=episode)
        request = IndexerMediaSearchRequest.create(**arguments)
        if request.season is None or request.episode is None:
            raise ValueError("season 和 episode 必须指定有效季集")
        return rank_episode_search(
            search, season=request.season, episode=request.episode, media=request,
            mapping_context=(audit or {}).get("mapping_context"), preferences=preferences,
        )

    @staticmethod
    @sdk_method('write')
    def download(result_ids: list[str], target: str = "preferred", *, verification_targets: list[dict] | None = None) -> dict:
        """在工作区为搜索返回的result_id列表生成整批下载预览确认卡，确认前不提交下载。省略target或preferred使用当前用户保存的下载目标，无偏好则光鸭；显式qb/guangya/both优先。预检冻结实际目标，确认前偏好变化不会改道。生成确认卡后结束脚本；确认后只执行冻结操作，不重跑脚本。确认回执data.items中的有效request_id可直接用activity.timeline("download", request_id)查询下载、整理和入库阶段；accepted仅表示受理。

        可选 verification_targets=[{result_id,query,tmdb_id?,season,episode,as_of?,library_name?}]。
        缺集下载传入完整目标，同一整季包可对应多集；服务端预检真实缺集，随下载请求持久化，
        后台等待下载与整理完成后逐目标核验入库。此目标清单不证明资源标题或文件确实覆盖缺集。
        """
        from app.indexers.downloads import prepare_download_verifications, preview_indexer_download_batch, submit_indexer_download_batch
        from app.indexers.runtime import get_indexer_service, run_indexer_awaitable_sync
        from app.modules.media_preferences import resolve_download_target

        if target == "preferred":
            target, _ = resolve_download_target(_owner())
        service = get_indexer_service()
        preview, snapshot = preview_indexer_download_batch(service, result_ids, target, scope="agent")
        if not preview["ok"]:
            return preview
        verifications = prepare_download_verifications(service, result_ids, verification_targets or [], scope="agent")
        return run_indexer_awaitable_sync(submit_indexer_download_batch(
            service, result_ids, target, expected_snapshot=snapshot, scope="agent",
            verification_targets=verifications or None, verification_owner=_owner() if verifications else "",
            origin_namespace="agent" if verifications else "indexer",
        ))


settings = Settings()
diagnostics = Diagnostics()
automation = Automation()
session = Session()
subscriptions = Subscriptions()
skills = Skills()
preferences = Preferences()
recognition = Recognition()
watchlist = Watchlist()
library = Library()
media = Media()
cloud = Cloud()
downloads = Downloads()
strm = Strm()
playback = Playback()
local = Local()
web = Web()
activity = Activity()
resources = Resources()

# 显式导出的是业务方法，不允许 RPC 访问模块、属性或任意 Python 对象。
rss = RSS()

METHODS, METHOD_EFFECTS = _collect_methods({
    "session": session,
    "settings": settings,
    "recognition": recognition,
    "subscriptions": subscriptions,
    "automation": automation,
    "library": library,
    "rss": rss,
    "diagnostics": diagnostics,
    "preferences": preferences,
    "skills": skills,
    "watchlist": watchlist,
    "media": media,
    "cloud": cloud,
    "downloads": downloads,
    "strm": strm,
    "playback": playback,
    "local": local,
    "web": web,
    "activity": activity,
    "resources": resources,
})

# 唯一分类声明派生执行集合，禁止另维护方法名单。
WRITE_METHODS = frozenset(name for name, effect in METHOD_EFFECTS.items() if effect == "write")


def prepare_sdk_method(method: str, arguments: dict, *, owner: str) -> dict:
    """宿主写入预检按真实签名归一参数，并在当前身份下解析首选服务器。"""
    bound = signature(METHODS[method]).bind(**arguments)
    bound.apply_defaults()
    values = {key: value for key, value in bound.arguments.items() if value is not Ellipsis}
    if (method.startswith("library.") or method == "media.refresh") and "server" in values:
        with call_owner(owner):
            values["server"] = Library._server(values["server"])
    return values


def describe_namespaces() -> dict[str, str]:
    """Python、CLI和工作区展示同一份业务范围说明。"""
    return {name: type(globals()[name]).__doc__ or ""
            for name in sorted({method.split(".")[0] for method in METHODS})}


def describe_methods() -> list[dict[str, str]]:
    """客户端与 --help 使用真实函数签名，不再维护一份模型专用 Schema。"""
    namespaces = describe_namespaces()
    return [{"name": name, "signature": str(signature(method, eval_str=True)), "doc": method.__doc__ or "",
             "namespace_doc": namespaces[name.split(".")[0]]}
            for name, method in METHODS.items()]
