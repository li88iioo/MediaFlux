"""把已验证的 MediaFlux 领域 action 映射为 Kernel 原子工具。

此层只复用领域能力，不承担自然语言编排；每个 handler 都由 ToolPipeline
统一完成校验、权限、Effect Gate、投影与提交。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from app.agent.confirmation_contract import build_confirmation_contract
from app.agent.feature_gate import (
    AgentRuntimeDisabled,
    agent_runtime_admission,
    current_agent_runtime_generation,
)
from app.agent.models import RiskLevel, ToolContext, ToolResult, ToolSpec
from app.agent.owner_routes import telegram_owner_route_is_currently_authorized
from app.agent.public_safety import sanitize_public_text

from ..capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from ..effects import PreparedEffect
from ..pipeline import ToolCallContext, ToolPipelineError

_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")

_DOMAIN_ALIASES = {
    "guangya": "cloud",
    "downloads": "download",
    "local_media": "local_media",
    "media_proxy": "playback",
    "strm": "automation",
}

# 这些是工具自身的检索语义，不是对用户消息做业务分流。
# 检索语义与领域 ToolSpec 一起声明，避免维护第二份意图路由。
_PREFIX_RETRIEVAL_TERMS: dict[str, tuple[str, ...]] = {
    "guangya": ("光鸭", "光鸭云盘", "云盘", "云端文件", "目录", "文件夹"),
    "provider": ("Jellyfin", "Emby", "qBittorrent", "qB", "媒体服务器", "实时"),
    "downloads": ("下载", "下载器", "qBittorrent", "qB", "任务", "进度"),
    "library": ("媒体库", "影片", "剧集", "集数", "入库"),
    "rss": ("RSS", "订阅源", "刷新周期", "排除关键字"),
    "media": ("媒体订阅", "追更", "订阅"),
    "indexer": ("资源", "资源搜索", "索引站", "种子", "磁力"),
    "discovery": ("推荐", "新剧", "电影", "剧集", "动漫", "国漫"),
    "web": ("联网", "网页", "链接", "公告", "文档", "最新", "定档", "公开信息"),
}

_TOOL_RETRIEVAL_TERMS: dict[str, tuple[str, ...]] = {
    "agent.capabilities": (
        "你是谁",
        "能做什么",
        "助手能力",
        "项目能力",
        "支持哪些功能",
    ),
    "guangya.fs.query": (
        "根目录",
        "列出目录",
        "目录内容",
        "查看目录",
        "浏览目录",
        "搜索文件",
        "查看文件夹",
        "子目录",
        "子目录名称",
        "文件夹名称",
        "目录下有哪些",
        "递归目录",
    ),
    "guangya.fs.change.preview": (
        "创建目录",
        "新建目录",
        "重命名",
        "改名",
        "移动",
        "复制",
        "拷贝",
        "备份到云盘目录",
        "回收站",
        "规整文件",
    ),
    "guangya.account.status": (
        "光鸭账号",
        "云盘容量",
        "剩余空间",
        "可用空间",
        "存储用量",
    ),
    "guangya.recycle.list": (
        "光鸭回收站",
        "回收站列表",
        "已删除文件",
        "垃圾箱内容",
    ),
    "guangya.recycle.restore": (
        "恢复回收站",
        "还原删除文件",
        "找回光鸭文件",
    ),
    "guangya.recycle.clear": (
        "清空回收站",
        "永久删除回收站",
        "彻底清理光鸭垃圾箱",
    ),
    "guangya.operation.status": (
        "光鸭任务状态",
        "恢复任务进度",
        "清空回收站进度",
    ),
    "guangya.share.list": (
        "光鸭分享列表",
        "我的分享",
        "已创建分享",
    ),
    "guangya.share.create": (
        "创建光鸭分享",
        "分享云盘文件",
        "生成分享链接",
        "提取码",
    ),
    "guangya.share.revoke": (
        "撤销光鸭分享",
        "取消分享链接",
        "删除分享",
    ),
    "provider.query": (
        "媒体总数",
        "媒体库统计",
        "媒体资源总量",
        "媒体库有多少资源",
        "媒体库有多少媒体",
        "电影剧集总数",
        "按媒体库统计",
        "指定媒体库数量",
        "动漫库有多少",
        "这个媒体库有多少部",
        "实时下载任务",
        "下载速度",
        "下载进度",
        "当前任务",
    ),
    "media.recommend_from_library": (
        "从媒体库推荐",
        "本地片单推荐",
        "今晚看什么",
        "今天心情很丧",
        "不用脑子",
        "爆笑",
        "无厘头",
        "日番",
        "推荐没看过的",
        "排除已看作品",
        "结合观看历史推荐",
        "Jellyfin 里值得看",
    ),
    "media.recently_played": (
        "最近播放",
        "播放历史",
        "观看历史",
        "最近看过",
        "我最近看了什么",
        "根据观看记录推荐",
        "根据最近播放推荐",
    ),
    "media.recently_added": (
        "最近入库",
        "最新入库",
        "最近添加",
        "媒体库最近新增",
    ),
    "media.continue_watching": (
        "继续观看",
        "没看完",
        "接着看",
        "Resume",
    ),
    "library.count_series_episodes": ("一共有多少集", "本地多少集", "季度分布"),
    "library.batch_presence": (
        "批量核对媒体库",
        "哪些在库",
        "缺哪几部",
        "缺少哪些电影",
        "片单收录情况",
        "导演作品在库情况",
    ),
    "indexer.search_resources": (
        "有没有资源",
        "搜索资源",
        "查找资源",
        "最近新出的步兵资源",
        "新出步兵",
        "无码资源",
        "uncensored",
        "Sukebei 最新",
        "成人资源索引",
    ),
    "rss.subscription_summaries": (
        "配置了哪些RSS",
        "RSS订阅列表",
        "RSS订阅源",
        "我订阅了哪些RSS",
        "我订阅了那些RSS",
        "有哪些RSS",
        "RSS规则",
    ),
    "media.subscription_summaries": ("配置了哪些媒体订阅", "媒体追更列表"),
    "rss.create_subscription": ("添加RSS", "创建RSS订阅", "新增RSS订阅"),
    "discovery.recommend": (
        "最近推荐",
        "新剧推荐",
        "国漫推荐",
        "片单推荐",
        "根据观看记录推荐",
        "根据最近播放推荐",
    ),
    "discovery.person_filmography": (
        "导演的所有电影",
        "导演全部作品",
        "人物电影作品表",
        "演员参演电影",
        "编剧作品",
        "按上映年份排列",
        "诺兰电影",
    ),
    "web.search": (
        "2026新剧",
        "最新定档",
        "近期公开资讯",
        "近期步兵资源",
        "最近新出资源",
        "成人作品近期发行信息",
    ),
    "web.read": (
        "打开网页",
        "读取链接",
        "网页正文",
        "总结网页",
        "官方公告",
        "查看文档",
        "fetch url",
    ),
}


def _production_owner_kind(owner: object) -> str:
    value = str(owner or "")
    if value.startswith("webk:v1:"):
        return "web"
    if value.startswith("tg:v1:"):
        return "telegram"
    return ""


def _bind_runtime_snapshot(fingerprint: str, context: ToolCallContext) -> str:
    if not _production_owner_kind(context.owner):
        return fingerprint
    return f"mfkr1:{current_agent_runtime_generation()}:{fingerprint}"


def _split_runtime_snapshot(
    fingerprint: str,
    context: ToolCallContext,
) -> tuple[int | None, str]:
    if not _production_owner_kind(context.owner):
        return None, fingerprint
    prefix, separator, remainder = str(fingerprint or "").partition(":")
    generation_text, separator2, domain_fingerprint = remainder.partition(":")
    if prefix != "mfkr1" or not separator or not separator2:
        raise ToolPipelineError("确认计划缺少运行态快照", code="confirmation_invalid")
    try:
        generation = int(generation_text)
    except (TypeError, ValueError) as exc:
        raise ToolPipelineError(
            "确认计划运行态快照无效", code="confirmation_invalid"
        ) from exc
    return generation, domain_fingerprint


def _kernel_context(context: ToolCallContext) -> ToolContext:
    return ToolContext(
        owner=context.owner,
        session_id=context.session_id,
        request_id=context.request_id,
        confirmation_bootstrap=False,
    )


def _public_error_message(value: object, *, fallback: str) -> str:
    return sanitize_public_text(value, limit=500) or fallback


def _safe_error(exc: Exception, *, fallback_code: str) -> ToolPipelineError:
    # 只有领域显式声明的 safe_message 才可跨越 ToolPipeline 边界；
    # 未预期异常的 str(exc) 可能包含绝对路径、URL 或凭据。
    message = _public_error_message(
        getattr(exc, "safe_message", ""),
        fallback="领域能力暂时不可用",
    )
    raw_code = str(getattr(exc, "code", "") or fallback_code).strip().casefold()
    code = raw_code if _ERROR_CODE_RE.fullmatch(raw_code) else fallback_code
    return ToolPipelineError(message, code=code)


def _assert_success(result: ToolResult, *, code: str) -> ToolResult:
    if not isinstance(result, ToolResult):
        raise ToolPipelineError("领域能力返回无效结果", code="invalid_tool_result")
    if not result.ok:
        message = _public_error_message(
            result.error or result.summary,
            fallback="领域能力未能完成请求",
        )
        domain_code = str(result.status or "").strip().casefold()
        # 领域服务已经把 conflict / busy / stale / outcome_unknown 等
        # 用户可处理状态标准化；Kernel 只校验其机器码形状，不应再次抹平。
        safe_code = domain_code if _ERROR_CODE_RE.fullmatch(domain_code) else code
        raise ToolPipelineError(message, code=safe_code)
    return result


def _domain(spec: ToolSpec) -> str:
    prefix = spec.name.partition(".")[0].casefold()
    candidate = spec.domains[0] if spec.domains else _DOMAIN_ALIASES.get(prefix, prefix)
    candidate = re.sub(r"[^a-z0-9_.-]+", "_", str(candidate).casefold()).strip("_.-")
    return candidate or "system"


def _effect(spec: ToolSpec) -> ToolEffect:
    if spec.risk is RiskLevel.READ and not spec.requires_confirmation:
        return ToolEffect.READ
    if spec.risk is RiskLevel.DANGER:
        return ToolEffect.DANGER
    return ToolEffect.WRITE


def _retrieval_terms(spec: ToolSpec) -> tuple[str, ...]:
    prefix = spec.name.partition(".")[0].casefold()
    values = (
        *spec.domains,
        spec.source_kind,
        *_PREFIX_RETRIEVAL_TERMS.get(prefix, ()),
        *_TOOL_RETRIEVAL_TERMS.get(spec.name, ()),
    )
    return tuple(
        dict.fromkeys(str(item).strip() for item in values if str(item).strip())
    )


def _related_tools(spec: ToolSpec) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(item).strip() for item in spec.related_tools if str(item).strip()
        )
    )


def adapt_tool_spec(spec: ToolSpec) -> KernelToolSpec:
    effect = _effect(spec)

    if effect is ToolEffect.READ:

        def read(arguments: dict[str, Any], context: ToolCallContext) -> ToolResult:
            try:
                if spec.context_handler is not None:
                    result = spec.context_handler(arguments, _kernel_context(context))
                elif spec.handler is not None:
                    result = spec.handler(arguments)
                else:  # pragma: no cover - 领域 ToolSpec 声明已保证
                    raise ToolPipelineError(
                        "领域能力不可执行", code="tool_not_executable"
                    )
            except ToolPipelineError:
                raise
            except Exception as exc:
                raise _safe_error(exc, fallback_code="tool_execution_failed") from exc
            if not isinstance(result, ToolResult):
                raise ToolPipelineError(
                    "领域能力返回无效结果", code="invalid_tool_result"
                )
            return result

        return KernelToolSpec(
            name=spec.name,
            model_name=spec.model_name,
            domain=_domain(spec),
            description=spec.description,
            examples=spec.examples,
            input_schema=spec.parameters,
            effect=effect,
            validator=spec.validator,
            read=read,
            cost=1.0 if spec.freshness != "live" else 1.25,
            metadata={
                "source_kind": spec.source_kind,
                "freshness": spec.freshness,
                "workflow": spec.workflow,
                "workflow_stage": spec.workflow_stage,
                "related_tools": _related_tools(spec),
                "retrieval_terms": _retrieval_terms(spec),
                "risk": spec.risk.value,
            },
        )

    def prepare(arguments: dict[str, Any], context: ToolCallContext) -> PreparedEffect:
        handler = spec.context_confirmation_preparer
        if handler is None:  # pragma: no cover - 领域 ToolSpec 声明已保证
            raise ToolPipelineError(
                "领域能力不支持预检", code="confirmation_not_supported"
            )
        try:
            result, fingerprint = handler(arguments, _kernel_context(context))
        except ToolPipelineError:
            raise
        except Exception as exc:
            raise _safe_error(exc, fallback_code="precondition_failed") from exc
        checked = _assert_success(result, code="precondition_failed")
        effect_metadata = (
            dict(checked.effect_metadata)
            if isinstance(checked.effect_metadata, dict)
            else {}
        )
        return PreparedEffect(
            preview=checked.to_dict(),
            snapshot_fingerprint=_bind_runtime_snapshot(
                str(fingerprint or ""), context
            ),
            metadata={
                **effect_metadata,
                "source_kind": spec.source_kind,
                "risk": spec.risk.value,
                "confirmation": build_confirmation_contract(
                    tool_name=spec.name,
                    risk=spec.risk,
                    preview=checked,
                ),
            },
        )

    def execute_confirmed(
        arguments: dict[str, Any],
        expected_snapshot: str,
        context: ToolCallContext,
    ) -> ToolResult:
        handler = spec.context_confirmed_handler
        if handler is None:  # pragma: no cover - 领域 ToolSpec 声明已保证
            raise ToolPipelineError(
                "领域能力不支持确认执行", code="confirmation_not_supported"
            )
        runtime_generation, domain_snapshot = _split_runtime_snapshot(
            expected_snapshot, context
        )
        owner_kind = _production_owner_kind(context.owner)
        try:
            if owner_kind:
                with agent_runtime_admission(
                    require_telegram=owner_kind == "telegram",
                    expected_generation=runtime_generation,
                ):
                    if (
                        owner_kind == "telegram"
                        and not telegram_owner_route_is_currently_authorized(
                            context.owner
                        )
                    ):
                        raise AgentRuntimeDisabled("Telegram Agent 授权已变化")
                    result = handler(
                        arguments, domain_snapshot, _kernel_context(context)
                    )
            else:
                result = handler(arguments, domain_snapshot, _kernel_context(context))
        except ToolPipelineError:
            raise
        except AgentRuntimeDisabled as exc:
            raise ToolPipelineError(
                "Media Agent 授权或运行状态已变化", code="agent_disabled"
            ) from exc
        except Exception as exc:
            raise _safe_error(exc, fallback_code="tool_execution_failed") from exc
        if not isinstance(result, ToolResult):
            raise ToolPipelineError("领域能力返回无效结果", code="invalid_tool_result")
        # 已确认写操作允许返回 conflict / busy / outcome_unknown 等可信终态。
        # Pipeline 会保留完整安全 DTO、完成审计，并由入口发布 effect.failed；
        # 只有异常或写后验证失败才进入 ToolPipelineError。
        return result

    def verify(arguments: dict[str, Any], value: Any, _context: ToolCallContext) -> Any:
        if spec.post_write_verifier is None:
            return value
        if not isinstance(value, ToolResult):
            raise ToolPipelineError(
                "写后验证输入无效", code="post_write_verification_failed"
            )
        try:
            verified = spec.post_write_verifier(arguments, value)
        except Exception as exc:
            raise _safe_error(
                exc, fallback_code="post_write_verification_failed"
            ) from exc
        return _assert_success(verified, code="post_write_verification_failed")

    return KernelToolSpec(
        name=spec.name,
        model_name=spec.model_name,
        domain=_domain(spec),
        description=spec.description,
        examples=spec.examples,
        input_schema=spec.parameters,
        effect=effect,
        validator=spec.validator,
        prepare=prepare,
        execute_confirmed=execute_confirmed,
        verify=verify if spec.post_write_verifier is not None else None,
        cost=2.0 if effect is ToolEffect.DANGER else 1.5,
        metadata={
            "source_kind": spec.source_kind,
            "freshness": spec.freshness,
            "workflow": spec.workflow,
            "workflow_stage": spec.workflow_stage,
            "related_tools": _related_tools(spec),
            "retrieval_terms": _retrieval_terms(spec),
            "risk": spec.risk.value,
        },
    )


def catalog_from_tool_specs(specs: Iterable[ToolSpec]) -> ToolCatalog:
    return ToolCatalog(adapt_tool_spec(spec) for spec in specs)
