"""确定性 Agent 编排器；后续 LLM 只能替换意图选择，不能绕过工具注册表。"""
from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
from datetime import date
import logging
import re
import secrets
import unicodedata
from typing import Any, Callable

from app import database as db
from app.agent.action_history import record_confirmation_error, record_confirmed_result
from app.agent.confirmation import ConfirmationStore
from app.agent.confirmation_contract import (
    build_confirmation_contract,
    sanitize_confirmation_contract,
)
from app.agent.models import LLMToolDisposition, RiskLevel, ToolContext, ToolResult
from app.agent.intents import ReadIntentSpec, match_read_intent
from app.agent import local_media_intents
from app.agent.indexer_config_actions import current_indexer_site_ids
from app.indexers.config import DEFAULT_INDEXER_SITE_IDS, INDEXER_SITE_ORDER
from app.agent.llm_router import (
    LLMReadPlan,
    LLMToolSelection,
    answer_conversation,
    compose_tool_answer,
    is_agent_action_request,
    is_compound_read_request,
    is_confirmation_planning_request,
    run_native_read_agent,
    select_confirmation_tool,
    select_orchestration_tool,
    select_read_tool,
    select_read_plan,
)
from app.agent.missing_media_workflows import (
    MissingMediaWorkflowRepository,
    is_missing_workflow_status_message,
    workflow_followup_context,
)
from app.agent.recent_patrol import RecentPatrolStore
from app.agent.recent_read_operations import READ_PLAN_OPERATION, RecentReadOperationStore
from app.agent.recent_resource_candidates import (
    RecentResourceCandidateStore,
    public_candidate_projection,
)
from app.agent.recent_discovery_candidates import RecentDiscoveryCandidateStore
from app.agent.recent_download_submissions import (
    RecentDownloadSubmissionStore,
    build_recent_download_library_verification,
    build_recent_download_status,
    explain_recent_download_status,
    parse_recent_download_verification_context,
    sanitize_submission_confirmation_result,
)
from app.agent.episode_audit import invalidate_episode_audit_cache
from app.agent.rate_limit import allow_agent_tool
from app.agent.registry import AgentToolError, ToolRegistry
from app.agent.rss_reference import resolve_rss_subscription_name
from app.agent import result_projection
from app.agent.session_context import AgentSessionContextRepository
from app.agent.workspace_next_actions import resolve_workspace_action_handoff

logger = logging.getLogger(__name__)
_QUERY_CONFIRMATION_EPOCH: ContextVar[tuple[str, int] | None] = ContextVar(
    "agent_query_confirmation_epoch", default=None
)
_QUERY_TOOL_RATE_IDENTITY: ContextVar[str] = ContextVar(
    "agent_query_tool_rate_identity", default=""
)
_LLM_FIRST_CONTEXT_MARKERS = (
    "这部", "这集", "这个", "它", "刚才", "上一个", "继续", "重试",
    "再来", "刷新一下", "打开它", "关闭它", "有多少", "缺不缺",
)


def _prefer_deterministic_context_route(
    message: str, conversation_context: list[dict[str, Any]] | None
) -> bool:
    """强指代继续由服务端绑定已验证上下文，模型不负责猜测对象。"""
    if not conversation_context:
        return False
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold()
    return any(marker in normalized for marker in _LLM_FIRST_CONTEXT_MARKERS)


class AgentInputError(ValueError):
    pass


def normalize_agent_message(value: Any) -> str:
    if not isinstance(value, str):
        raise AgentInputError("消息必须是字符串")
    message = unicodedata.normalize("NFKC", value).strip()
    if not message or len(message) > 1000:
        raise AgentInputError("消息必须为 1 到 1000 个字符")
    if any(unicodedata.category(char).startswith("C") for char in message):
        raise AgentInputError("消息不能包含控制字符")
    return message


_AMBIGUOUS_FOLLOWUP_PHRASES = frozenset({
    "关注一下", "关注一下啥情况", "关注一下什么情况", "啥", "啥情况", "什么情况",
    "咋回事", "怎么回事", "怎么样了", "然后呢", "继续", "继续看看", "看一下", "看看",
    "这是什么情况", "这个什么情况", "帮我看看这个", "这个呢", "再说具体点",
    "说具体点", "说清楚点", "详细一点", "具体是什么", "具体怎么回事",
    "为什么", "为啥", "咋会这样", "怎么会这样", "那怎么办", "怎么办",
    "如何处理", "怎么处理", "该怎么办", "那这个呢",
    "打开它", "开启它", "启用它", "关闭它", "关掉它", "停用它", "禁用它",
    "把它打开", "把它开启", "把它启用", "把它关闭", "把它关掉", "把它停用",
})
_CASUAL_GREETING_PHRASES = frozenset({
    "你好", "您好", "嗨", "哈喽", "hello", "hi", "hey",
    "在吗", "在不在", "在干嘛", "在干什么", "在干嘛呢", "在干什么呢",
})
_AMBIGUOUS_FOLLOWUP_MARKERS = (
    "啥情况", "什么情况", "怎么回事", "咋回事", "继续看看", "看看这个",
    "说具体", "说清楚", "详细一点", "具体是什么", "然后呢", "这个呢",
    "为什么", "为啥", "怎么办", "怎么处理", "如何处理",
)
_FOLLOWUP_DOMAIN_ANCHORS = (
    "下载", "队列", "缺集", "巡检", "媒体库", "jellyfin", "emby", "rss", "订阅",
    "strm", "整理", "光鸭", "云盘", "配置", "设置", "资源站", "索引器", "资源",
    "本地媒体", "自动化", "调度", "telegram", "通知", "系统简报", "整体状态",
)
_BROAD_FOLLOWUP_TOOLS = frozenset({
    "workspace.briefing", "workspace.health", "workspace.next_actions", "workspace.todo",
    "agent.read_plan",
})


def _is_ambiguous_followup(message: str) -> bool:
    normalized = re.sub(r"[\s，。！？!?、；;：:]+", "", message.casefold())
    if any(token in normalized for token in _FOLLOWUP_DOMAIN_ANCHORS):
        return False
    if re.search(r"第\d+(?:个|项|条)?(?:任务|结果|记录)", normalized):
        return False
    return normalized in _AMBIGUOUS_FOLLOWUP_PHRASES or (
        len(normalized) <= 20
        and any(token in normalized for token in _AMBIGUOUS_FOLLOWUP_MARKERS)
    )


def _is_casual_greeting(message: str) -> bool:
    normalized = re.sub(r"[\s，。！？!?、；;：:~～]+", "", message.casefold())
    return normalized in _CASUAL_GREETING_PHRASES


def _is_vague_multi_site_request(message: str) -> bool:
    normalized = _normalize_indexer_command(message)
    site_scope = any(token in normalized for token in (
        "多个站点", "更多站点",
        "多个资源站", "更多资源站",
    ))
    action = any(token in normalized for token in (
        "开启", "启用", "打开", "增加", "添加", "关闭", "停用", "禁用", "移除", "删除", "去掉",
    ))
    search_scope = any(token in normalized for token in ("搜索", "检索", "找资源", "资源站"))
    # “把所有站点都打开”虽未说出“资源站”，也必须停在澄清层；绝不能据此猜测
    # 网络服务、媒体服务器或成人站点等其他含义。
    return site_scope and action and (search_scope or "站点" in normalized)


def _last_assistant_context(
    conversation_context: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    for item in reversed(conversation_context or []):
        if isinstance(item, dict) and str(item.get("role") or "").casefold() == "assistant":
            return item
    return {}


def _latest_assistant_tool_context(
    conversation_context: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """返回当前连续话题中最近一条真正执行过工具的 assistant 上下文。

    历史通常按 user/assistant 成对排列。这里仅允许越过同一结果附带的一条
    assistant 解释；若最后一条历史已经是新的 user 消息，就不再回溯旧工具，
    避免“刷新一下 / 重试”误操作很早以前的对象。
    """
    context = [item for item in (conversation_context or []) if isinstance(item, dict)]
    if context and str(context[-1].get("role") or "").casefold() == "user":
        return {}
    for item in reversed(context):
        role = str(item.get("role") or "").casefold()
        if role == "assistant" and str(item.get("tool_name") or "").strip():
            return item
        if role == "user":
            break
    return {}


_MEDIA_RATING_RETRY_PHRASES = frozenset({
    "重试", "再试一次", "再查一次", "重新查询", "重新查", "继续查", "再查查",
})
_MEDIA_RATING_TYPE_TOKENS = (
    ("电视剧", "tv"), ("电视连续剧", "tv"), ("连续剧", "tv"),
    ("剧集", "tv"), ("这部剧", "tv"), ("该剧", "tv"),
    ("电影", "movie"), ("影片", "movie"), ("这部电影", "movie"),
)
_MEDIA_RATING_YEAR_SEARCH_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_MEDIA_RATING_QUOTED_TITLE_RE = re.compile(r"[《「『\"']([^》」』\"']{1,120})[》」』\"']")


def _safe_media_context(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    title = " ".join(str(value.get("title") or "").split()).strip()
    if not title or len(title) > 160:
        return {}
    result = {"title": title}
    original_title = " ".join(str(value.get("original_title") or "").split()).strip()
    if original_title and len(original_title) <= 160:
        result["original_title"] = original_title
    year = str(value.get("year") or "").strip()
    if re.fullmatch(r"(?:19|20)\d{2}", year):
        result["year"] = year
    media_type = str(value.get("media_type") or "").strip().lower()
    if media_type in {"movie", "tv"}:
        result["media_type"] = media_type
    return result


def _latest_media_context(
    conversation_context: list[dict[str, Any]] | None,
) -> dict[str, str]:
    """只继承当前主题最近一次助手回复中的媒体上下文。

    一旦最近的助手回复已经切换到下载、RSS、配置等其他领域，就不能继续
    向前翻找旧片名，否则“搜索这部电影资源”会错误复用几轮之前的作品。
    """
    for item in reversed(conversation_context or []):
        if not isinstance(item, dict):
            continue
        if str(item.get("role") or "").casefold() not in {"assistant", "summary"}:
            continue
        media_context = _safe_media_context(item.get("media_context"))
        if media_context:
            return media_context
        if str(item.get("tool_name") or "").strip() or str(item.get("text") or "").strip():
            return {}
    return {}


_MEDIA_CONTEXT_REFERENCES = frozenset({
    "它", "本剧", "该剧", "这剧", "这个剧", "这部剧",
    "本电视剧", "该电视剧", "这部电视剧",
    "本电影", "该电影", "这个电影", "这部电影",
    "本作品", "该作品", "这个作品", "这部作品",
})
_GENERIC_MEDIA_COLLECTION_TERMS = frozenset({
    "我追的剧", "我的追剧", "追剧", "在追的剧", "在追",
    "我追的番", "我的追番", "追番", "在追的番",
    "我追的", "我的追更", "追更", "追更列表",
    "我的订阅", "已订阅", "订阅列表",
    "想看", "想看的剧", "想看的电影", "我的想看", "想看列表",
    "待看", "待看的剧", "更新", "最新更新", "最近更新", "新剧", "新番",
})


def _is_generic_media_collection_term(value: Any) -> bool:
    normalized = re.sub(
        r"[\s，。！？!?、；;：:~～]+",
        "",
        unicodedata.normalize("NFKC", str(value or "")).casefold(),
    )
    return normalized in _GENERIC_MEDIA_COLLECTION_TERMS


def _contextual_media_reference_type(value: Any) -> str:
    normalized = re.sub(
        r"[\s，。！？?、:：]+",
        "",
        unicodedata.normalize("NFKC", str(value or "")).casefold(),
    )
    if normalized in {"本电影", "该电影", "这个电影", "这部电影", "影片"}:
        return "movie"
    if normalized in {
        "本剧", "该剧", "这剧", "这个剧", "这部剧",
        "本电视剧", "该电视剧", "这部电视剧", "剧集",
    }:
        return "tv"
    return ""


def _is_contextual_media_query(value: Any) -> bool:
    normalized = re.sub(
        r"[\s，。！？?、:：]+",
        "",
        unicodedata.normalize("NFKC", str(value or "")).casefold(),
    )
    return not normalized or normalized in _MEDIA_CONTEXT_REFERENCES


def _inherit_verified_media_query(
    arguments: dict[str, Any],
    conversation_context: list[dict[str, Any]] | None,
    *,
    tv_only: bool = False,
) -> dict[str, Any]:
    """为明确的媒体追问继承最近一次已核验标题，不覆盖本轮显式输入。"""
    inherited = dict(arguments)
    query = str(inherited.get("query") or "").strip()
    if not _is_contextual_media_query(query):
        return inherited
    requested_type = _contextual_media_reference_type(query)
    inherited.pop("query", None)
    media_context = _latest_media_context(conversation_context)
    title = str(media_context.get("title") or "").strip()
    media_type = str(media_context.get("media_type") or "").strip().casefold()
    if (
        not title
        or (tv_only and media_type == "movie")
        or (requested_type and media_type and requested_type != media_type)
    ):
        return inherited
    inherited["query"] = title
    return inherited


def _match_media_identity(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(char for char in text if char.isalnum())


def _extract_media_rating_title(message: str) -> str:
    quoted = _MEDIA_RATING_QUOTED_TITLE_RE.search(message)
    if quoted is not None:
        return " ".join(quoted.group(1).split()).strip()[:120]
    text = unicodedata.normalize("NFKC", str(message or "")).strip()
    text = re.sub(
        r"^(?:请|麻烦|帮我|请帮我|请问|查一下|查询一下|查询|查查|查|看看|看一下|帮我查一下)+",
        "",
        text,
    )
    text = _MEDIA_RATING_YEAR_SEARCH_RE.sub(" ", text)
    text = re.sub(
        r"(?:的)?(?:豆瓣(?:电影)?)?(?:评分|打分)(?:是多少|多少|怎么样|如何|是几分|几分)?",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"(?:豆瓣|电视剧|电视连续剧|连续剧|剧集|电影|影片|这部剧|该剧|这部电影)", " ", text)
    text = re.sub(r"(?:是多少|多少分|是几分|几分|怎么样|如何|重试|再试一次|重新查询|重新查|继续查)", " ", text)
    text = re.sub(r"[\s，。！？!?、；;：:~～·•\-_/]+", " ", text).strip(" 的")
    title = " ".join(text.split()).strip()[:120]
    return "" if _is_generic_media_collection_term(title) else title


def _pending_rating_media_context(
    conversation_context: list[dict[str, Any]] | None,
) -> dict[str, str]:
    """恢复一次失败评分查询所使用的显式媒体身份。

    失败结果不会写入已核验 ``media_context``，这是正确的安全边界；但用户紧接着
    说“电视剧 / 重试”时，仍应能继续修正刚才明确输入的片名，而不是退回更早的
    无关作品。这里只读取最近评分调用之前的少量用户文本，不把工具错误文本当身份。
    """
    context = list(conversation_context or [])
    rating_index = -1
    for index in range(len(context) - 1, -1, -1):
        item = context[index]
        if not isinstance(item, dict):
            continue
        if (
            str(item.get("role") or "").casefold() == "assistant"
            and str(item.get("tool_name") or "") == "discovery.lookup_rating"
        ):
            rating_index = index
            break
    if rating_index < 0:
        return {}
    for item in reversed(context[max(0, rating_index - 6):rating_index]):
        if not isinstance(item, dict) or str(item.get("role") or "").casefold() != "user":
            continue
        text = str(item.get("text") or item.get("message") or item.get("content") or "").strip()
        if not text:
            continue
        title = _extract_media_rating_title(text)
        compact = re.sub(r"[\s，。！？!?、；;：:~～]+", "", text.casefold())
        if not title or (
            "评分" not in compact
            and "豆瓣" not in compact
            and not any(token in compact for token, _kind in _MEDIA_RATING_TYPE_TOKENS)
            and not _MEDIA_RATING_YEAR_SEARCH_RE.search(text)
        ):
            continue
        result = {"title": title}
        for token, kind in _MEDIA_RATING_TYPE_TOKENS:
            if token in compact:
                result["media_type"] = kind
                break
        year = _MEDIA_RATING_YEAR_SEARCH_RE.search(text)
        if year:
            result["year"] = year.group(1)
        return result
    return {}


def contextual_media_rating_request(
    message: str,
    conversation_context: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """解析评分续问；只继承已验签的媒体身份，不依赖模型猜测。"""
    text = unicodedata.normalize("NFKC", str(message or "")).strip()
    compact = re.sub(r"[\s，。！？!?、；;：:~～]+", "", text.casefold())
    last_assistant = _latest_assistant_tool_context(conversation_context)
    previous_rating = str(last_assistant.get("tool_name") or "") == "discovery.lookup_rating"
    media_context = (
        _pending_rating_media_context(conversation_context)
        if previous_rating
        else {}
    ) or _latest_media_context(conversation_context)

    explicit_rating = "豆瓣" in compact and any(
        token in compact for token in ("评分", "打分", "多少分", "几分")
    )
    contextual_rating = bool(media_context) and any(
        token in compact for token in ("这部剧评分", "这部电影评分", "它的评分", "评分多少", "多少分")
    )
    retry = previous_rating and compact in _MEDIA_RATING_RETRY_PHRASES
    correction = (
        previous_rating
        and not explicit_rating
        and not retry
        and len(compact) <= 80
        and (
            any(token in compact for token, _kind in _MEDIA_RATING_TYPE_TOKENS)
            or bool(_MEDIA_RATING_YEAR_SEARCH_RE.search(text))
        )
    )
    if not (explicit_rating or contextual_rating or retry or correction):
        return None

    media_type = ""
    for token, kind in _MEDIA_RATING_TYPE_TOKENS:
        if token in compact:
            media_type = kind
            break
    year_match = _MEDIA_RATING_YEAR_SEARCH_RE.search(text)
    year = year_match.group(1) if year_match else ""
    extracted_title = "" if retry else _extract_media_rating_title(text)
    refers_to_previous = (
        retry
        or not extracted_title
        or extracted_title in {"这部", "这部剧", "这部电影", "它", "该剧"}
        or _match_media_identity(extracted_title) == _match_media_identity(
            media_context.get("title", "")
        )
    )
    title = media_context.get("title", "") if refers_to_previous else extracted_title
    # 只有真正指代上一部媒体时才继承类型和年份；明确输入新标题不能串用旧身份。
    if refers_to_previous:
        if not media_type:
            media_type = media_context.get("media_type", "")
        if not year:
            year = media_context.get("year", "")
    if not title:
        return None

    arguments: dict[str, Any] = {
        "query": title,
        "allow_web_fallback": True,
    }
    if media_type in {"movie", "tv"}:
        arguments["media_type"] = media_type
    if re.fullmatch(r"(?:19|20)\d{2}", year):
        arguments["year"] = year
    return arguments


_FOLLOWUP_REASON_PHRASES = frozenset({
    "为什么", "为啥", "怎么回事", "咋回事", "为什么会这样", "怎么会这样",
    "具体怎么回事", "这是什么情况", "这个什么情况",
})
_FOLLOWUP_ACTION_PHRASES = frozenset({
    "那怎么办", "怎么办", "该怎么办", "如何处理", "怎么处理",
})
_FOLLOWUP_CONTINUE_PHRASES = frozenset({
    "继续", "继续看看", "然后呢", "这个呢", "那这个呢",
    "再说具体点", "说具体点", "说清楚点", "详细一点",
})


def _followup_intent(message: str) -> str | None:
    normalized = re.sub(
        r"[\s，。！？!?、；;：:~～]+",
        "",
        unicodedata.normalize("NFKC", str(message or "")).casefold(),
    )
    if normalized in _FOLLOWUP_REASON_PHRASES:
        return "reason"
    if normalized in _FOLLOWUP_ACTION_PHRASES:
        return "action"
    if normalized in _FOLLOWUP_CONTINUE_PHRASES:
        return "continue"
    return None


_RECENT_READ_RETRY_PHRASES = frozenset({
    "重试", "再试一次", "再查一次", "重新查询", "重新查",
    "继续查", "再查查", "刷新一下结果",
})


def is_recent_read_retry_message(message: str) -> bool:
    """只接受明确、短小的重试续句，绝不把写操作当成可重放请求。"""
    normalized = re.sub(
        r"[\s，。！？!?、；;：:~～]+",
        "",
        unicodedata.normalize("NFKC", str(message or "")).casefold(),
    )
    return normalized in _RECENT_READ_RETRY_PHRASES


_RESOURCE_SEARCH_TOKENS = ("资源", "种子", "磁力", "下载源", "资源站")
_RESOURCE_SEARCH_VERBS = ("找", "搜", "搜索", "查找", "有没有", "有无")
_DISCOVERY_SEARCH_TOKENS = (
    "网上", "外部", "影视探索", "探索页", "tmdb", "豆瓣", "bangumi", "bgm",
)
_DISCOVERY_SEARCH_VERBS = _RESOURCE_SEARCH_VERBS + ("查", "查询", "查一下")
_LOCAL_LIBRARY_TOKENS = ("媒体库", "jellyfin", "emby", "我的库", "本地库", "库里")
_DISCOVERY_RECOMMEND_ANCHORS = ("推荐", "有什么好看的", "看什么", "片荒", "剧荒")
_DISCOVERY_RECOMMEND_REJECT_TOKENS = _RESOURCE_SEARCH_TOKENS + _LOCAL_LIBRARY_TOKENS
_RECENT_RESOURCE_REFERENCES = (
    "刚才推荐", "刚刚推荐", "最近推荐", "上次推荐",
    "刚才资源结果", "刚刚资源结果", "最近资源结果", "上次资源结果",
    "刚才的推荐", "刚刚的推荐", "最近的推荐", "上次的推荐",
    "刚才搜索", "刚刚搜索", "最近搜索", "上次搜索",
    "刚才的搜索", "搜索结果", "刚才搜索结果", "上面的资源",
    "上面结果", "刚才那个资源", "刚才那条资源",
)
_RECENT_RESOURCE_ACTIONS = ("下载", "推送", "提交", "发送")
_RECENT_RESOURCE_REJECT_TOKENS = (
    "不要", "别", "取消", "停止", "不准", "无需", "不用",
    "了吗", "是否", "状态", "进度", "什么意思", "怎么", "如何", "为什么",
    "能否", "可以吗", "会不会",
)
_RECENT_DOWNLOAD_REFERENCES = ("刚才", "刚刚", "上次", "最近")
_RECENT_DOWNLOAD_SCOPES = ("下载", "推送", "提交", "资源", "任务")
_RECENT_DOWNLOAD_DOMAIN_REJECT_TOKENS = (
    "下载队列", "任务队列", "下载器", "qb 下载", "qb下载",
    "rss", "订阅", "strm",
    "光鸭整理", "云盘整理", "本地媒体", "qb 下载任务", "qb下载任务",
    "qbittorrent 下载任务", "怎么配置", "如何配置", "配置", "设置",
)
_RECENT_DOWNLOAD_REJECT_TOKENS = (
    "不要", "取消", "停止", "重试", "再试", "重新提交", "再次提交", "删除", "修复",
    *_RECENT_DOWNLOAD_DOMAIN_REJECT_TOKENS,
)
_RECENT_DOWNLOAD_EXPLANATION_REJECT_TOKENS = (
    *_RECENT_DOWNLOAD_REJECT_TOKENS,
    "下载任务", "下载自动化", "qbittorrent",
)
_RECENT_DOWNLOAD_EXPLANATION_MARKERS = (
    "为什么", "为何", "原因", "哪里异常", "怎么失败", "怎么回事",
    "卡在哪里", "卡在哪", "出什么问题", "问题在哪",
)
_RECENT_DOWNLOAD_STATUS_MARKERS = (
    "状态", "进度", "到哪", "到哪了", "到哪一步", "成功了吗", "完成了吗",
    "有没有成功", "是否成功", "怎么样", "怎样了", "结束了吗",
)
_RECENT_DOWNLOAD_LIBRARY_MARKERS = ("入库", "补齐", "补上", "缺集")
_RECENT_DOWNLOAD_LIBRARY_INTENTS = ("核验", "检查", "确认", "是否", "了吗", "有没有", "有无")
_RECENT_DOWNLOAD_LIBRARY_REJECT_TOKENS = (
    *_RECENT_DOWNLOAD_REJECT_TOKENS,
    "暂停", "恢复", "开始下载", "执行入库", "帮我入库", "帮我补齐",
)
_DISCOVERY_TV_TOKENS = ("电视剧", "剧集", "连续剧", "电视节目", "剧荒")
_DISCOVERY_MOVIE_TOKENS = ("电影", "影片", "片荒")
_DISCOVERY_CONTEXTUAL_RECOMMEND_TOKENS = ("类似", "相似", "同类", "根据", "按照", "基于", "我喜欢", "适合我")
_BANGUMI_CALENDAR_CONTENT_TOKENS = ("番剧", "新番", "动画", "bangumi", "bgm")
_BANGUMI_CALENDAR_MARKERS = ("日历", "放送", "每日放送")
_BANGUMI_TODAY_TOKENS = ("今天", "今日")
_BANGUMI_WEEK_TOKENS = ("本周", "这周")
_BANGUMI_CALENDAR_LIST_INTENTS = ("有什么", "有哪些", "哪些", "看什么")
_WEEKDAY_ALIASES = {
    1: ("周一", "星期一", "礼拜一"),
    2: ("周二", "星期二", "礼拜二"),
    3: ("周三", "星期三", "礼拜三"),
    4: ("周四", "星期四", "礼拜四"),
    5: ("周五", "星期五", "礼拜五"),
    6: ("周六", "星期六", "礼拜六"),
    7: ("周日", "周天", "星期日", "星期天", "礼拜日", "礼拜天"),
}
_WORKSPACE_SEARCH_ANCHORS = (
    "全局搜索", "全局搜", "工作区搜索", "工作区搜", "项目内搜索", "项目内搜", "控制台搜索", "控制台搜",
    "下载记录", "下载历史", "下载任务", "整理日志", "订阅记录", "rss 订阅", "rss订阅",
    "本地媒体", "本地整理", "本地入库",
)
_WORKSPACE_GLOBAL_ANCHORS = (
    "全局搜索", "全局搜", "工作区搜索", "工作区搜", "项目内搜索", "项目内搜", "控制台搜索", "控制台搜",
)
_WORKSPACE_TRACE_ANCHORS = ("流程追踪", "处理到哪", "走到哪一步", "到哪一步")
_WORKSPACE_SEARCH_VERBS = ("找", "搜", "搜索", "查找", "查询", "查一下", "有没有", "有无")
_WORKSPACE_BRIEFING_ANCHORS = (
    "每日简报", "系统简报", "工作区简报", "今日概览", "运行概览", "全局状态概览", "系统概览",
)
_WORKSPACE_BRIEFING_REJECT_TOKENS = (
    "配置", "设置", "开启", "关闭", "启用", "停用", "禁用", "运行一次", "执行",
    "修复", "重试", "删除", "清理", "扫描", "刷新",
)

_WORKSPACE_HEALTH_ANCHORS = (
    "媒体健康总检", "媒体系统健康", "媒体系统总检", "媒体系统全面检查", "整个媒体系统",
)
_WORKSPACE_HEALTH_INTENTS = ("总检", "全面检查", "健康", "状态", "怎么样")
_WORKSPACE_HEALTH_REJECT_TOKENS = (
    "配置", "设置", "开启", "关闭", "启用", "停用", "禁用", "执行", "运行一次",
    "刷新", "扫描", "清理", "删除", "重试", "jellyfin", "emby", "媒体服务器",
    "strm", "光鸭", "云盘", "整理任务", "下载队列", "下载任务", "rss", "订阅",
    "本地媒体", "索引器", "资源站",
)

_AGENT_ACTION_HISTORY_ANCHORS = (
    "agent 操作历史", "agent操作历史", "agent 执行历史", "agent执行历史",
    "agent 操作记录", "agent操作记录", "确认动作历史", "受确认动作历史",
    "刚才 agent 做了什么", "刚才agent做了什么", "刚才执行了什么",
)


def agent_action_history_request(message: str) -> dict[str, Any] | None:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    explicit_anchor = any(anchor in normalized for anchor in _AGENT_ACTION_HISTORY_ANCHORS)
    generic_agent_history = (
        "agent" in normalized
        and any(token in normalized for token in ("操作历史", "执行历史", "操作记录", "执行记录", "确认动作"))
    )
    if not explicit_anchor and not generic_agent_history:
        return None
    outcome = "all"
    if any(token in normalized for token in ("失败", "未成功", "异常")):
        outcome = "failed"
    elif any(token in normalized for token in ("成功", "已完成")):
        outcome = "success"
    limit = 20
    match = re.search(r"(?:最近|近)?\s*(\d{1,3})\s*(?:条|次)", normalized)
    if match:
        limit = int(match.group(1))
        if limit < 1 or limit > 50:
            raise AgentInputError("Agent 操作历史查询条数必须为 1 到 50")
    return {"limit": limit, "outcome": outcome}


def is_workspace_health_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if any(token in normalized for token in _WORKSPACE_HEALTH_REJECT_TOKENS):
        return False
    if not any(anchor in normalized for anchor in _WORKSPACE_HEALTH_ANCHORS):
        return False
    if not any(intent in normalized for intent in _WORKSPACE_HEALTH_INTENTS):
        return False
    specialized_checks = (
        is_download_queue_diagnosis_message,
        is_indexer_readiness_diagnosis_message,
        is_rss_diagnosis_message,
        is_strm_failure_triage_message,
        local_media_intents.is_local_media_diagnosis_message,
        is_automation_pipeline_diagnosis_message,
        is_media_server_diagnosis_message,
        is_media_server_test_message,
        is_missing_season_resource_search_message,
        is_missing_episode_resource_search_message,
        is_episode_audit_message,
        is_library_update_check_message,
    )
    return not any(check(normalized) for check in specialized_checks)


def is_workspace_briefing_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if is_workspace_health_message(normalized):
        return False
    if any(token in normalized for token in _WORKSPACE_BRIEFING_REJECT_TOKENS):
        return False
    if not any(anchor in normalized for anchor in _WORKSPACE_BRIEFING_ANCHORS):
        return False
    specialized_checks = (
        is_download_queue_diagnosis_message,
        is_indexer_readiness_diagnosis_message,
        is_rss_diagnosis_message,
        is_strm_failure_triage_message,
        local_media_intents.is_local_media_diagnosis_message,
        is_automation_pipeline_diagnosis_message,
        is_missing_season_resource_search_message,
        is_missing_episode_resource_search_message,
        is_episode_audit_message,
        is_library_update_check_message,
    )
    return not any(check(normalized) for check in specialized_checks)


_WORKSPACE_NEXT_ACTIONS_ANCHORS = (
    "工作区下一步",
    "系统下一步",
    "全局下一步",
    "接下来该做什么",
    "接下来做什么",
    "下一步做什么",
    "接下来优先处理什么",
    "现在该处理什么",
    "下一步行动",
)
_WORKSPACE_NEXT_ACTIONS_REJECT_TOKENS = (
    "配置", "设置", "开启", "关闭", "启用", "停用", "执行", "运行", "修复",
    "重试", "删除", "清理", "下载", "提交", "搜索", "查找", "rss", "订阅",
    "strm", "光鸭", "云盘", "网盘", "本地媒体", "媒体库", "缺集", "更新",
    "资源", "种子", "磁力", "健康", "总检", "诊断", "简报",
)


def is_workspace_next_actions_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if any(token in normalized for token in _WORKSPACE_NEXT_ACTIONS_REJECT_TOKENS):
        return False
    return any(anchor in normalized for anchor in _WORKSPACE_NEXT_ACTIONS_ANCHORS)


_WORKSPACE_TODO_ANCHORS = (
    "统一待办", "工作区待办", "全局待办", "待办概览", "待办中心",
)
_WORKSPACE_TODO_REJECT_TOKENS = (
    "strm", "rss", "订阅", "下载", "云盘", "网盘", "光鸭", "整理", "本地媒体",
    "媒体库", "jellyfin", "emby", "tmdb", "豆瓣", "bangumi", "bgm", "资源",
    "种子", "磁力", "缺集", "更新", "能力", "help", "工具列表", "开启", "关闭",
    "启用", "停用", "运行", "执行", "预览", "搜索", "查找",
)


def is_workspace_todo_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if any(token in normalized for token in _WORKSPACE_TODO_REJECT_TOKENS):
        return False
    return any(anchor in normalized for anchor in _WORKSPACE_TODO_ANCHORS)


_MEDIA_SERVER_TEST_VERBS = ("测试", "检查", "验证", "校验", "诊断", "连通", "健康", "状态", "可用", "能用", "正常")
_MEDIA_SERVER_TEST_SCOPES = ("连接", "连通", "服务器", "服务", "配置", "jellyfin", "emby")
_MEDIA_SERVER_DIAGNOSIS_SCOPES = ("媒体服务器", "媒体服务", "媒体节点", "jellyfin", "emby")
_MEDIA_SERVER_DIAGNOSIS_INTENTS = (
    "诊断", "健康", "状态", "就绪", "可用", "能用", "正常", "异常", "兼容", "版本", "配置对",
)
_MEDIA_SERVER_COMPATIBILITY_MARKERS = (
    "兼容", "版本", "类型", "节点", "槽位", "10.x", "jellyfin 10", "jellyfin10", "jellyfin 12", "jellyfin12",
)
_MEDIA_SERVER_DIAGNOSIS_REJECT_TOKENS = (
    "strm", "缺集", "更新", "资源", "种子", "磁力", "播放", "打开", "搜索", "查找", "启用", "关闭", "删除",
    "媒体库", "电影", "剧集",
)
_DOWNLOAD_DIAGNOSIS_SCOPES = ("下载队列", "下载任务", "下载自动化", "qbittorrent", "qb 下载", "qb下载", "下载器")
_DOWNLOAD_DIAGNOSIS_INTENTS = ("诊断", "检查", "查看", "哪些", "状态", "卡住", "停滞", "异常", "失败", "排队")
_DOWNLOAD_DIAGNOSIS_REJECT_TOKENS = ("配置", "设置")
_CONFIG_DIAGNOSIS_EXACT_PHRASES = frozenset({
    "项目诊断", "环境诊断", "系统诊断", "配置诊断", "诊断项目", "诊断配置",
    "诊断系统", "请诊断", "进行诊断", "检查配置", "检查项目配置", "项目配置检查",
    "检查系统配置",
})
_CONFIG_DIAGNOSIS_SCOPES = (
    "项目配置", "系统配置", "环境配置", "服务配置", "配置", "设置", "项目", "环境", "系统",
)
_CONFIG_DIAGNOSIS_INTENTS = (
    "检查", "诊断", "健康", "状态", "正常", "异常", "就绪", "完整性", "核对", "校验",
)
_CONFIG_DIAGNOSIS_REJECT_TOKENS = (
    "怎么", "如何", "为什么", "修改", "更改", "设置成", "配置成", "保存", "开启", "关闭",
    "启用", "停用", "联网搜索", "搜索", "找", "教程", "指南", "说明", "帮助", "重置",
)
_DOWNLOAD_CONTROL_PATTERN = re.compile(
    r"^(暂停|停止|恢复|继续|删除|移除)\s*(?:q(?:bittorrent|b)?\s*任务|下载任务)\s*[《「『\"'](.+?)[》」』\"']$",
    re.IGNORECASE,
)
_DOWNLOAD_CONTROL_TOOLS = {
    "暂停": "downloads.pause_task",
    "停止": "downloads.pause_task",
    "恢复": "downloads.resume_task",
    "继续": "downloads.resume_task",
    "删除": "downloads.delete_task",
    "移除": "downloads.delete_task",
}
_DOWNLOAD_RETRY_SCOPES = (
    "下载请求", "下载待处理请求", "下载待处理记录", "待处理下载请求", "待处理下载记录",
)
_DOWNLOAD_RETRY_VERBS = ("重新提交", "再提交", "重试", "重新投递", "再次投递")


def download_retry_submission_request(message: str) -> dict[str, Any] | None:
    """只解析同时含明确记录编号与唯一目标的下载重投命令。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if not normalized:
        return None
    if not any(scope in normalized for scope in _DOWNLOAD_RETRY_SCOPES):
        return None
    if not any(verb in normalized for verb in _DOWNLOAD_RETRY_VERBS):
        return None
    id_match = re.search(
        r"(?:下载(?:待处理)?(?:请求|记录)|待处理下载(?:请求|记录))\s*#?\s*(\d{1,12})",
        normalized,
    )
    if id_match is None:
        return None
    request_id = int(id_match.group(1))
    if request_id < 1:
        return None

    has_qb = bool(re.search(r"(?:^|[^a-z0-9])q(?:b|bittorrent)(?:$|[^a-z0-9])", normalized))
    has_guangya = "光鸭" in normalized
    explicit_both = any(token in normalized for token in ("两者", "两个目标", "两边", "both"))
    if explicit_both or (has_qb and has_guangya):
        target = "both"
    elif has_qb:
        target = "qb"
    elif has_guangya:
        target = "guangya"
    else:
        return None
    return {"request_id": request_id, "target": target}


def is_download_retry_submission_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    return any(scope in normalized for scope in _DOWNLOAD_RETRY_SCOPES) and any(
        verb in normalized for verb in _DOWNLOAD_RETRY_VERBS
    )


def is_config_diagnosis_message(message: str) -> bool:
    """只在用户明确要求检查系统配置时执行全局配置诊断。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    normalized = normalized.strip(" ，。！？!?、；;：:~～")
    if not normalized or any(token in normalized for token in _CONFIG_DIAGNOSIS_REJECT_TOKENS):
        return False
    if normalized in _CONFIG_DIAGNOSIS_EXACT_PHRASES:
        return True
    return any(scope in normalized for scope in _CONFIG_DIAGNOSIS_SCOPES) and any(
        intent in normalized for intent in _CONFIG_DIAGNOSIS_INTENTS
    )


def is_download_queue_diagnosis_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold()
    if any(token in normalized for token in _DOWNLOAD_DIAGNOSIS_REJECT_TOKENS):
        return False
    return any(scope in normalized for scope in _DOWNLOAD_DIAGNOSIS_SCOPES) and any(
        intent in normalized for intent in _DOWNLOAD_DIAGNOSIS_INTENTS
    )


def download_task_control_request(message: str) -> tuple[str, dict[str, str]] | None:
    normalized = unicodedata.normalize("NFKC", str(message or "")).strip()
    matched = _DOWNLOAD_CONTROL_PATTERN.fullmatch(normalized)
    if not matched:
        return None
    verb, task_name = matched.groups()
    task_name = " ".join(task_name.split()).strip()
    if not task_name:
        return None
    return _DOWNLOAD_CONTROL_TOOLS[verb], {"task_name": task_name}


def is_download_task_control_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold()
    return any(token in normalized for token in ("下载任务", "qb 任务", "qb任务", "qbittorrent 任务")) and any(
        token in normalized for token in ("暂停", "停止", "恢复", "继续", "删除", "移除")
    )


_RSS_DIAGNOSIS_SCOPES = ("rss", "rss 订阅", "rss订阅", "rss 条目", "rss条目", "订阅源")
_RSS_DIAGNOSIS_INTENTS = ("诊断", "检查", "查看", "状态", "健康", "异常", "失败", "待处理", "积压", "卡住")
_RSS_DIAGNOSIS_REJECT_TOKENS = ("配置", "设置")

_INDEXER_READINESS_SCOPES = (
    "资源站", "资源站搜索", "资源检索", "资源搜索", "多站资源搜索", "索引站", "索引器",
)
_INDEXER_READINESS_INTENTS = (
    "诊断", "检查", "查看", "状态", "健康", "就绪", "可用", "能用", "正常",
    "异常", "失败", "不可用", "连通",
)
_INDEXER_READINESS_REJECT_TOKENS = (
    "配置", "设置", "开启", "关闭", "启用", "停用", "禁用", "新增", "删除",
    "修改", "保存", "重置",
)


def is_indexer_readiness_diagnosis_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if any(anchor in normalized for anchor in _WORKSPACE_SEARCH_ANCHORS + _WORKSPACE_TRACE_ANCHORS):
        return False
    if any(token in normalized for token in _DISCOVERY_SEARCH_TOKENS):
        return False
    if any(token in normalized for token in _INDEXER_READINESS_REJECT_TOKENS):
        return False
    if re.search(r"(?:找|搜|搜索|查找)\s*[《「『\"']", normalized):
        return False
    return any(scope in normalized for scope in _INDEXER_READINESS_SCOPES) and any(
        intent in normalized for intent in _INDEXER_READINESS_INTENTS
    )


def is_rss_diagnosis_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if any(token in normalized for token in _RSS_DIAGNOSIS_REJECT_TOKENS):
        return False
    return any(scope in normalized for scope in _RSS_DIAGNOSIS_SCOPES) and any(
        intent in normalized for intent in _RSS_DIAGNOSIS_INTENTS
    )


_RSS_SUBSCRIPTION_SUMMARY_PATTERN = re.compile(
    r"^(?:请(?:帮我)?\s*)?"
    r"(?:(?:查看|检查|查询|诊断|看看|显示)\s*)?"
    r"rss\s*(?:订阅)?\s*(?:(?:id|编号)\s*)?[#:]?\s*(\d{1,9})\s*"
    r"(?:的\s*)?(?:状态|详情|摘要|情况|健康|概览|列表|是否启用|刷新周期|刷新间隔|调度状态)?"
    r"[.!。！？]?$",
    re.IGNORECASE,
)
_RSS_SUBSCRIPTION_SUMMARY_READ_TOKENS = (
    "查看", "检查", "查询", "诊断", "看看", "显示", "列出", "状态", "详情", "摘要", "情况", "健康",
    "概览", "列表", "是否启用", "刷新周期", "刷新间隔", "调度状态",
)
_RSS_SUBSCRIPTION_SUMMARIES_TOKENS = (
    "全部", "所有", "列表", "列出", "逐个", "每个", "各个", "摘要", "概览", "汇总",
)
_RSS_SUBSCRIPTION_BULK_TOKENS = (
    "全部", "所有", "列表", "列出", "逐个", "每个", "各个", "汇总",
)
_RSS_SUBSCRIPTION_GENERIC_SUMMARY_NAMES = frozenset({
    "状态", "详情", "摘要", "情况", "健康", "概览", "订阅", "rss", "rss订阅",
    "是否启用", "刷新周期", "刷新间隔", "调度状态",
})
_RSS_SUBSCRIPTION_DIAGNOSTIC_PSEUDO_NAMES = (
    "自动化流程", "自动化链路", "刷新失败", "刷新异常", "刷新故障",
    "待处理", "积压", "卡住",
)
_RSS_SUBSCRIPTION_READ_REJECT_TOKENS = (
    "停用", "禁用", "关闭", "删除", "设置为", "设为", "改为", "调整为",
    "下载", "提交", "推送", "重试", "创建", "新增", "编辑", "修改", "保存",
)
_RSS_SUBSCRIPTION_NUMBER_SEARCH_PATTERN = re.compile(
    r"rss\s*(?:订阅)?\s*(?:(?:id|编号)\s*)?[#:]?\s*\d{1,9}",
    re.IGNORECASE,
)


def rss_subscription_summary_request(message: str) -> dict[str, int] | None:
    """解析一个精确编号的 RSS 订阅只读摘要请求。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if any(token in normalized for token in _RSS_SUBSCRIPTION_READ_REJECT_TOKENS):
        return None
    if not any(token in normalized for token in _RSS_SUBSCRIPTION_SUMMARY_READ_TOKENS):
        return None
    matched = _RSS_SUBSCRIPTION_SUMMARY_PATTERN.fullmatch(normalized)
    if not matched:
        return None
    subscription_id = int(matched.group(1))
    return {"subscription_id": subscription_id} if subscription_id > 0 else None


_RSS_SUBSCRIPTION_NAMED_SUMMARY_PATTERNS = (
    re.compile(
        r"^(?:请(?:帮我)?\s*)?(?:查看|检查|查询|诊断|看看|显示)\s*"
        r"(?P<name>.+?)\s*(?:的\s*)?rss\s*(?:订阅)?\s*"
        r"(?:状态|详情|摘要|情况|健康|概览|是否启用|刷新周期|刷新间隔|调度状态)?[.!。！？]?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:请(?:帮我)?\s*)?(?:查看|检查|查询|诊断|看看|显示)\s*"
        r"rss\s*(?:订阅)?\s*(?P<name>.+?)\s*"
        r"(?:状态|详情|摘要|情况|健康|概览|是否启用|刷新周期|刷新间隔|调度状态)?[.!。！？]?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:请(?:帮我)?\s*)?(?P<name>.+?)\s*rss\s*(?:订阅)?\s*"
        r"(?:状态|详情|摘要|情况|健康|概览)[.!。！？]?$",
        re.IGNORECASE,
    ),
)


def rss_subscription_summary_name(message: str) -> str | None:
    """解析一个明确 RSS 名称的只读摘要请求。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).strip()
    lowered = normalized.casefold()
    if any(token in lowered for token in _RSS_SUBSCRIPTION_READ_REJECT_TOKENS):
        return None
    if any(token in lowered for token in _RSS_SUBSCRIPTION_BULK_TOKENS):
        return None
    for pattern in _RSS_SUBSCRIPTION_NAMED_SUMMARY_PATTERNS:
        matched = pattern.fullmatch(normalized)
        if not matched:
            continue
        name = _clean_rss_named_target(matched.group("name"))
        if not name or name.casefold() in _RSS_SUBSCRIPTION_GENERIC_SUMMARY_NAMES:
            return None
        # “RSS 自动化流程状态 / RSS 刷新失败状态 / RSS 待处理”描述的是
        # RSS 子系统诊断维度，不是订阅名称；保留真实命名订阅的摘要路由。
        if any(token in name.casefold() for token in _RSS_SUBSCRIPTION_DIAGNOSTIC_PSEUDO_NAMES):
            return None
        return name
    return None


def is_rss_subscription_summaries_message(message: str) -> bool:
    """判断用户是否明确要求列出全部 RSS 订阅的安全摘要。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if "rss" not in normalized:
        return False
    if any(token in normalized for token in _RSS_SUBSCRIPTION_READ_REJECT_TOKENS):
        return False
    if _RSS_SUBSCRIPTION_NUMBER_SEARCH_PATTERN.search(normalized):
        return False
    # “查看 Mikan RSS 订阅摘要”是单订阅请求；只有解析不到明确名称时，
    # 才允许“摘要/概览”等词进入全部订阅汇总。
    if rss_subscription_summary_name(normalized) is not None:
        return False
    return any(token in normalized for token in _RSS_SUBSCRIPTION_SUMMARIES_TOKENS) and any(
        token in normalized for token in _RSS_SUBSCRIPTION_SUMMARY_READ_TOKENS
    )


_RSS_REFRESH_REQUEST_PATTERN = re.compile(
    r"^(?:请(?:帮我)?\s*)?刷新\s*rss\s*(?:订阅)?\s*"
    r"(?:(?:id|编号)\s*)?[#:]?\s*(\d{1,9})\s*(?:一次|一下)?[.!。！]?$",
    re.IGNORECASE,
)


def is_rss_subscription_refresh_write_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if is_rss_diagnosis_message(normalized):
        return False
    return "刷新" in normalized and "rss" in normalized


def is_rss_recent_activity_message(message: str) -> bool:
    """识别用户对最近一天 RSS 下载次数的明确查询。"""
    normalized = re.sub(
        r"\s+", "", unicodedata.normalize("NFKC", str(message or "")).casefold()
    )
    if "rss" not in normalized:
        return False
    recent_window = any(token in normalized for token in (
        "近24小时", "最近24小时", "过去24小时", "24小时内", "24小时",
        "近24h", "最近24h", "过去24h", "24h内",
        "近一天", "最近一天", "过去一天",
    ))
    activity_intent = any(token in normalized for token in (
        "下载", "下载量", "统计", "情况", "活动", "成功",
    ))
    asks_count = any(token in normalized for token in (
        "几次", "多少次", "多少个", "数量", "下载了几", "成功了几",
        "统计", "情况", "下载量", "活动",
    ))
    return recent_window and activity_intent and asks_count


def rss_subscription_refresh_request(message: str) -> dict[str, int] | None:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    matched = _RSS_REFRESH_REQUEST_PATTERN.fullmatch(normalized)
    if not matched:
        return None
    subscription_id = int(matched.group(1))
    if subscription_id <= 0:
        return None
    return {"subscription_id": subscription_id}


_RSS_REFRESH_NAMED_PATTERNS = (
    re.compile(
        r"^(?:请(?:帮我)?\s*)?刷新(?:一下)?\s*(?P<name>.+?)\s*(?:的\s*)?"
        r"rss\s*(?:订阅)?\s*(?:一次|一下)?[.!。！]?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:请(?:帮我)?\s*)?刷新(?:一下)?\s*rss\s*(?:订阅)?\s*"
        r"(?P<name>.+?)\s*(?:一次|一下)?[.!。！]?$",
        re.IGNORECASE,
    ),
)
_RSS_NAMED_TARGET_REJECTS = frozenset({
    "全部", "所有", "全部订阅", "所有订阅", "all", "everything",
    "一下", "一次", "一下rss", "rss", "订阅", "rss订阅",
})


def _clean_rss_named_target(value: str) -> str | None:
    name = str(value or "").strip(" \t\r\n'\"“”‘’《》<>【】[]()（）")
    if not name or name.casefold() in _RSS_NAMED_TARGET_REJECTS:
        return None
    if re.fullmatch(r"(?:(?:id|编号)\s*)?[#:]?\s*\d{1,9}", name, re.IGNORECASE):
        return None
    return name


def rss_subscription_refresh_name(message: str) -> str | None:
    """解析明确包含 RSS 语境的用户可见订阅名称。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).strip()
    for pattern in _RSS_REFRESH_NAMED_PATTERNS:
        matched = pattern.fullmatch(normalized)
        if not matched:
            continue
        return _clean_rss_named_target(matched.group("name"))
    return None


_RSS_SUBSCRIPTION_ENABLED_PATTERN = re.compile(
    r"^(?:请(?:帮我)?\s*)?(启用|开启|停用|禁用|关闭)\s*rss\s*(?:订阅)?\s*"
    r"(?:(?:id|编号)\s*)?[#:]?\s*(\d{1,9})\s*(?:一下)?[.!。！]?$",
    re.IGNORECASE,
)
_RSS_SUBSCRIPTION_INTERVAL_PATTERN = re.compile(
    r"^(?:请(?:帮我)?\s*)?(?:把|将)\s*rss\s*(?:订阅)?\s*"
    r"(?:(?:id|编号)\s*)?[#:]?\s*(\d{1,9})\s*"
    r"(?:的\s*)?(?:自动)?刷新(?:周期|间隔)\s*(?:设置为|设为|改为|调整为)\s*"
    r"(\d{1,5})\s*(分钟|分|小时|时)[.!。！]?$",
    re.IGNORECASE,
)
_RSS_SUBSCRIPTION_DELETE_PATTERN = re.compile(
    r"^(?:请(?:帮我)?\s*)?(?:永久)?删除\s*rss\s*(?:订阅)?\s*"
    r"(?:(?:id|编号)\s*)?[#:]?\s*(\d{1,9})\s*(?:一下)?[.!。！]?$",
    re.IGNORECASE,
)
_RSS_SUBSCRIPTION_NAMED_ENABLED_PATTERNS = (
    re.compile(
        r"^(?:请(?:帮我)?\s*)?(启用|开启|停用|禁用|关闭)\s*(?P<name>.+?)\s*"
        r"(?:的\s*)?rss\s*(?:订阅)?\s*(?:一下)?[.!。！]?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:请(?:帮我)?\s*)?(启用|开启|停用|禁用|关闭)\s*rss\s*(?:订阅)?\s*"
        r"(?P<name>.+?)\s*(?:一下)?[.!。！]?$",
        re.IGNORECASE,
    ),
)
_RSS_SUBSCRIPTION_NAMED_INTERVAL_PATTERNS = (
    re.compile(
        r"^(?:请(?:帮我)?\s*)?(?:把|将)\s*(?P<name>.+?)\s*(?:的\s*)?rss\s*(?:订阅)?\s*"
        r"(?:的\s*)?(?:自动)?刷新(?:周期|间隔)\s*(?:设置为|设为|改为|调整为)\s*"
        r"(?P<amount>\d{1,5})\s*(?P<unit>分钟|分|小时|时)[.!。！]?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:请(?:帮我)?\s*)?(?:把|将)\s*rss\s*(?:订阅)?\s*(?P<name>.+?)\s*"
        r"(?:的\s*)?(?:自动)?刷新(?:周期|间隔)\s*(?:设置为|设为|改为|调整为)\s*"
        r"(?P<amount>\d{1,5})\s*(?P<unit>分钟|分|小时|时)[.!。！]?$",
        re.IGNORECASE,
    ),
)
_RSS_SUBSCRIPTION_NAMED_DELETE_PATTERNS = (
    re.compile(
        r"^(?:请(?:帮我)?\s*)?(?:永久)?删除\s*(?P<name>.+?)\s*(?:的\s*)?"
        r"rss\s*(?:订阅)?\s*(?:一下)?[.!。！]?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:请(?:帮我)?\s*)?(?:永久)?删除\s*rss\s*(?:订阅)?\s*"
        r"(?P<name>.+?)\s*(?:一下)?[.!。！]?$",
        re.IGNORECASE,
    ),
)


def rss_subscription_control_name_request(
    message: str,
) -> tuple[str, str, dict[str, Any]] | None:
    """解析单个 RSS 订阅名称，内部工具仍只接收 subscription_id。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).strip()
    for pattern in _RSS_SUBSCRIPTION_NAMED_ENABLED_PATTERNS:
        matched = pattern.fullmatch(normalized)
        if not matched:
            continue
        name = _clean_rss_named_target(matched.group("name"))
        if name is None:
            return None
        return (
            "rss.set_subscription_enabled",
            name,
            {"enabled": matched.group(1) in {"启用", "开启"}},
        )
    for pattern in _RSS_SUBSCRIPTION_NAMED_INTERVAL_PATTERNS:
        matched = pattern.fullmatch(normalized)
        if not matched:
            continue
        name = _clean_rss_named_target(matched.group("name"))
        if name is None:
            return None
        amount = int(matched.group("amount"))
        unit = matched.group("unit")
        interval = amount * 60 if unit in {"小时", "时"} else amount
        if interval > 10_080:
            return None
        return (
            "rss.set_refresh_interval",
            name,
            {"refresh_interval_minutes": interval},
        )
    for pattern in _RSS_SUBSCRIPTION_NAMED_DELETE_PATTERNS:
        matched = pattern.fullmatch(normalized)
        if not matched:
            continue
        name = _clean_rss_named_target(matched.group("name"))
        if name is None:
            return None
        return "rss.delete_subscription", name, {}
    return None


_RSS_SUBSCRIPTION_CONTROL_TOKENS = (
    "启用", "开启", "停用", "禁用", "关闭", "删除", "刷新周期", "刷新间隔",
)


def rss_subscription_control_request(
    message: str,
) -> tuple[str, dict[str, Any]] | None:
    """只解析单订阅、精确 ID 的管理命令；不会接受批量或模糊对象。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    enabled_match = _RSS_SUBSCRIPTION_ENABLED_PATTERN.fullmatch(normalized)
    if enabled_match:
        subscription_id = int(enabled_match.group(2))
        if subscription_id <= 0:
            return None
        return (
            "rss.set_subscription_enabled",
            {
                "subscription_id": subscription_id,
                "enabled": enabled_match.group(1) in {"启用", "开启"},
            },
        )
    interval_match = _RSS_SUBSCRIPTION_INTERVAL_PATTERN.fullmatch(normalized)
    if interval_match:
        subscription_id = int(interval_match.group(1))
        amount = int(interval_match.group(2))
        unit = interval_match.group(3)
        interval = amount * 60 if unit in {"小时", "时"} else amount
        if subscription_id <= 0 or interval > 10_080:
            return None
        return (
            "rss.set_refresh_interval",
            {
                "subscription_id": subscription_id,
                "refresh_interval_minutes": interval,
            },
        )
    delete_match = _RSS_SUBSCRIPTION_DELETE_PATTERN.fullmatch(normalized)
    if delete_match:
        subscription_id = int(delete_match.group(1))
        if subscription_id <= 0:
            return None
        return "rss.delete_subscription", {"subscription_id": subscription_id}
    return None


def is_rss_subscription_control_write_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if is_rss_diagnosis_message(normalized):
        return False
    return "rss" in normalized and any(
        token in normalized for token in _RSS_SUBSCRIPTION_CONTROL_TOKENS
    )


_MEDIA_SUBSCRIPTION_SCOPE_PATTERN = (
    r"(?:(?:媒体|影视)(?:追更)?订阅|追更订阅|我追的剧|我的追剧|在追的剧|"
    r"我追的番|我的追番|在追的番|我的追更|我的订阅|订阅列表|追番|追剧|追更)"
)
_MEDIA_SUBSCRIPTION_COLLECTION_SCOPES = (
    "我追的剧", "我的追剧", "在追的剧", "我追的番", "我的追番",
    "在追的番", "我的追更", "我的订阅", "订阅列表", "追番", "追剧", "追更",
)
_MEDIA_SUBSCRIPTION_BARE_SUMMARY_QUERIES = frozenset({
    "我的订阅", "我的追番", "我的追剧", "我追的剧", "我追的番",
    "追更列表", "订阅列表", "追番列表", "追剧列表",
})
_MEDIA_SUBSCRIPTION_SUMMARY_PATTERN = re.compile(
    rf"^(?:请(?:帮我)?\s*)?"
    rf"(?:(?:查看|检查|查询|诊断|看看|显示)\s*)?"
    rf"(?:{_MEDIA_SUBSCRIPTION_SCOPE_PATTERN})\s*"
    r"(?:(?:id|编号)\s*)?[#:]?\s*(\d{1,9})\s*"
    r"(?:的\s*)?(?:状态|详情|摘要|情况|健康|概览|是否启用|缺集情况)?"
    r"[.!。！？]?$",
    re.IGNORECASE,
)
_MEDIA_SUBSCRIPTION_ENABLED_PATTERN = re.compile(
    rf"^(?:请(?:帮我)?\s*)?(暂停|停用|禁用|关闭|恢复|启用|开启)\s*"
    rf"(?:{_MEDIA_SUBSCRIPTION_SCOPE_PATTERN})\s*"
    r"(?:(?:id|编号)\s*)?[#:]?\s*(\d{1,9})\s*(?:一下)?[.!。！]?$",
    re.IGNORECASE,
)
_MEDIA_SUBSCRIPTION_READ_TOKENS = (
    "查看", "检查", "查询", "诊断", "看看", "显示", "列出", "状态", "详情", "摘要", "情况", "健康",
    "概览", "列表", "是否启用", "缺集",
)
_MEDIA_SUBSCRIPTION_LIST_TOKENS = (
    "全部", "所有", "列表", "列出", "逐个", "每个", "各个", "摘要", "概览", "汇总",
)
_MEDIA_SUBSCRIPTION_WRITE_TOKENS = (
    "暂停", "停用", "禁用", "关闭", "恢复", "启用", "开启",
)
_MEDIA_SUBSCRIPTION_UPDATE_QUERY_TOKENS = (
    "有没有", "有无", "了吗", "吗", "么", "情况", "检查", "看看", "查询", "又更新",
)
_MEDIA_SUBSCRIPTION_UPDATE_EXCLUDED_TOKENS = (
    "rss", "订阅源", "刷新", "间隔", "频率", "设置", "配置", "修改",
    *_MEDIA_SUBSCRIPTION_WRITE_TOKENS,
)


def _has_media_subscription_scope(message: str) -> bool:
    return bool(re.search(_MEDIA_SUBSCRIPTION_SCOPE_PATTERN, message, re.IGNORECASE))


def media_subscription_summary_request(message: str) -> dict[str, int] | None:
    """解析单条媒体追更订阅的只读摘要请求。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if "rss" in normalized or any(token in normalized for token in _MEDIA_SUBSCRIPTION_WRITE_TOKENS):
        return None
    if not any(token in normalized for token in _MEDIA_SUBSCRIPTION_READ_TOKENS):
        return None
    matched = _MEDIA_SUBSCRIPTION_SUMMARY_PATTERN.fullmatch(normalized)
    if not matched:
        return None
    subscription_id = int(matched.group(1))
    return {"subscription_id": subscription_id} if subscription_id > 0 else None


def is_media_subscription_summaries_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if "rss" in normalized or not _has_media_subscription_scope(normalized):
        return False
    if any(token in normalized for token in _MEDIA_SUBSCRIPTION_WRITE_TOKENS):
        return False
    if re.search(r"(?:id|编号)?\s*[#:]?\s*\d{1,9}", normalized):
        return False
    bare_query = normalized.strip(" ，。！？!?、；;：:~～")
    if bare_query in _MEDIA_SUBSCRIPTION_BARE_SUMMARY_QUERIES:
        return True
    has_read_intent = any(token in normalized for token in _MEDIA_SUBSCRIPTION_READ_TOKENS)
    has_collection_scope = any(
        token in normalized for token in _MEDIA_SUBSCRIPTION_COLLECTION_SCOPES
    )
    return has_read_intent and (
        any(token in normalized for token in _MEDIA_SUBSCRIPTION_LIST_TOKENS)
        or has_collection_scope
    )


def is_media_subscription_updates_message(message: str) -> bool:
    """识别“我的订阅有更新吗”一类实时媒体追更核对，不与 RSS 配置混淆。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if not normalized or any(
        token in normalized for token in _MEDIA_SUBSCRIPTION_UPDATE_EXCLUDED_TOKENS
    ):
        return False
    if not (_has_media_subscription_scope(normalized) or "订阅" in normalized):
        return False
    if not any(token in normalized for token in ("更新", "新一集", "新一季", "新内容")):
        return False
    return any(token in normalized for token in _MEDIA_SUBSCRIPTION_UPDATE_QUERY_TOKENS)


def media_subscription_control_request(
    message: str,
) -> tuple[str, dict[str, Any]] | None:
    """只接受单条、精确编号的媒体追更暂停或恢复命令。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if "rss" in normalized:
        return None
    matched = _MEDIA_SUBSCRIPTION_ENABLED_PATTERN.fullmatch(normalized)
    if not matched:
        return None
    subscription_id = int(matched.group(2))
    if subscription_id <= 0:
        return None
    return (
        "media.set_subscription_enabled",
        {
            "subscription_id": subscription_id,
            "enabled": matched.group(1) in {"恢复", "启用", "开启"},
        },
    )


def is_media_subscription_control_write_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    return (
        "rss" not in normalized
        and _has_media_subscription_scope(normalized)
        and any(token in normalized for token in _MEDIA_SUBSCRIPTION_WRITE_TOKENS)
    )


_RSS_PENDING_DOWNLOAD_TOKENS = ("待处理", "积压", "pending")
_RSS_PENDING_DOWNLOAD_INTENTS = ("下载", "提交", "推送")
_RSS_PENDING_DOWNLOAD_REJECT_TOKENS = (
    "全部", "所有", "光鸭", "失败", "重试", "刷新", "创建", "新增",
    "编辑", "修改", "删除", "移除", "标记", "已处理",
)
_RSS_PENDING_DOWNLOAD_READ_TOKENS = (
    "查看", "检查", "诊断", "状态", "统计", "汇总", "原因", "怎么样", "有没有",
)


def is_rss_pending_download_write_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if any(token in normalized for token in _RSS_PENDING_DOWNLOAD_READ_TOKENS):
        return False
    return (
        any(scope in normalized for scope in _RSS_DIAGNOSIS_SCOPES)
        and any(token in normalized for token in _RSS_PENDING_DOWNLOAD_TOKENS)
        and any(intent in normalized for intent in _RSS_PENDING_DOWNLOAD_INTENTS)
    )


def rss_pending_download_request(message: str) -> dict[str, int] | None:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if not is_rss_pending_download_write_message(normalized):
        return None
    if any(token in normalized for token in _RSS_PENDING_DOWNLOAD_REJECT_TOKENS):
        return None
    matched = re.search(r"(?<!\d)(\d{1,3})(?!\d)", normalized)
    limit = int(matched.group(1)) if matched else 10
    if not 1 <= limit <= 20:
        return None
    return {"limit": limit}


_RSS_FAILURE_RETRY_TOKENS = ("失败", "故障", "异常", "错误")
_RSS_FAILURE_RETRY_INTENTS = ("重试", "再试", "重新提交", "再次提交")
_RSS_FAILURE_RETRY_REJECT_TOKENS = (
    "全部", "所有", "光鸭", "待处理", "刷新", "创建", "新增",
    "编辑", "修改", "删除", "移除", "标记", "已处理",
)
_RSS_FAILURE_RETRY_READ_TOKENS = (
    "查看", "检查", "诊断", "状态", "统计", "汇总", "原因", "怎么样", "有没有",
)


def is_rss_failure_retry_write_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if any(token in normalized for token in _RSS_FAILURE_RETRY_READ_TOKENS):
        return False
    return (
        any(scope in normalized for scope in _RSS_DIAGNOSIS_SCOPES)
        and any(token in normalized for token in _RSS_FAILURE_RETRY_TOKENS)
        and any(intent in normalized for intent in _RSS_FAILURE_RETRY_INTENTS)
    )


def rss_failure_retry_request(message: str) -> dict[str, int] | None:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if not is_rss_failure_retry_write_message(normalized):
        return None
    if any(token in normalized for token in _RSS_FAILURE_RETRY_REJECT_TOKENS):
        return None
    matched = re.search(r"(?<!\d)(\d{1,3})(?!\d)", normalized)
    limit = int(matched.group(1)) if matched else 10
    if not 1 <= limit <= 20:
        return None
    return {"limit": limit}


_STRM_FAILURE_SCOPES = ("strm",)
_STRM_FAILURE_TOKENS = ("失败", "故障", "异常", "错误")
_STRM_FAILURE_READ_INTENTS = ("诊断", "检查", "查看", "统计", "汇总", "原因", "怎么了", "状态", "分诊")
_STRM_FAILURE_WRITE_INTENTS = ("重试", "再试", "重新处理", "再次处理", "修复", "清理", "删除", "移除", "解决", "恢复")


def _normalize_intent_message(message: str) -> str:
    return unicodedata.normalize("NFKC", str(message or "")).casefold().strip()


_DANGEROUS_ACTION_DISCUSSION_TOKENS = (
    "不想", "不要", "别", "不许", "无需", "不用",
    "如果", "假如", "要是",
    "能不能", "能否", "可以吗", "是否", "为什么", "怎么", "如何", "会不会",
    "查看", "查询", "检查", "状态", "情况", "怎么样",
    "吗", "呢", "？", "?",
)


def _is_dangerous_action_discussion(message: str) -> bool:
    normalized = _normalize_intent_message(message)
    return any(token in normalized for token in _DANGEROUS_ACTION_DISCUSSION_TOKENS)


def is_strm_failure_write_message(message: str) -> bool:
    normalized = _normalize_intent_message(message)
    if _is_dangerous_action_discussion(normalized):
        return False
    return (
        any(scope in normalized for scope in _STRM_FAILURE_SCOPES)
        and any(token in normalized for token in _STRM_FAILURE_TOKENS)
        and any(intent in normalized for intent in _STRM_FAILURE_WRITE_INTENTS)
    )


def strm_failure_retry_request(message: str) -> dict[str, str] | None:
    normalized = _normalize_intent_message(message)
    if _is_dangerous_action_discussion(normalized):
        return None
    if not (
        any(scope in normalized for scope in _STRM_FAILURE_SCOPES)
        and any(token in normalized for token in _STRM_FAILURE_TOKENS)
        and any(intent in normalized for intent in ("重试", "再试", "重新处理", "再次处理"))
    ):
        return None
    if any(token in normalized for token in ("删除", "清理", "移除", "修复", "解决", "恢复")):
        return None
    if any(token in normalized for token in ("元数据", "字幕", "图片", "nfo")):
        scope = "metadata"
    elif "生成" in normalized:
        scope = "generate"
    else:
        scope = "all"
    return {"scope": scope}


def is_strm_failure_triage_message(message: str) -> bool:
    normalized = _normalize_intent_message(message)
    if is_strm_failure_write_message(normalized):
        return False
    return (
        any(scope in normalized for scope in _STRM_FAILURE_SCOPES)
        and any(token in normalized for token in _STRM_FAILURE_TOKENS)
        and any(intent in normalized for intent in _STRM_FAILURE_READ_INTENTS)
    )


_ORGANIZE_AUDIT_SCOPES = ("整理记录", "整理日志", "整理历史")
_ORGANIZE_AUDIT_READ_INTENTS = ("查看", "检查", "列出", "摘要", "汇总", "最近", "失败", "历史", "记录")
_ORGANIZE_AUDIT_WRITE_INTENTS = (
    "开始", "执行", "运行", "停止", "清理", "删除", "撤销", "重试", "修改", "设置",
)


def organize_audit_request(message: str) -> dict[str, Any] | None:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    # 本地媒体的待确认与终态历史有更精确的聚合工具，避免被宽泛的整理审计路由抢占。
    if (
        local_media_intents.is_local_media_review_queue_summary_message(normalized)
        or local_media_intents.is_local_media_history_summary_message(normalized)
    ):
        return None
    explicit_scope = any(scope in normalized for scope in _ORGANIZE_AUDIT_SCOPES)
    generic_record_scope = "整理" in normalized and any(
        token in normalized
        for token in (
            "记录", "日志", "历史", "失败", "成功", "完成", "跳过",
            "回退", "回滚", "待确认", "人工", "处理中", "进行中", "最近",
        )
    )
    if (
        not (explicit_scope or generic_record_scope)
        or not any(intent in normalized for intent in _ORGANIZE_AUDIT_READ_INTENTS)
        or any(intent in normalized for intent in _ORGANIZE_AUDIT_WRITE_INTENTS)
    ):
        return None
    origin = "all"
    if "光鸭" in normalized or "云盘" in normalized:
        origin = "guangya"
    elif "本地" in normalized:
        origin = "local"
    status = "all"
    for token, value in (
        ("待确认", "manual"),
        ("人工", "manual"),
        ("失败", "failed"),
        ("处理中", "processing"),
        ("进行中", "processing"),
        ("成功", "success"),
        ("完成", "success"),
        ("跳过", "skipped"),
        ("回退", "reverted"),
        ("回滚", "reverted"),
    ):
        if token in normalized:
            status = value
            break
    limit = 20 if any(token in normalized for token in ("最近", "历史", "记录", "日志")) else 10
    return {"origin": origin, "status": status, "limit": limit}


_AUTOMATION_DIAGNOSIS_SCOPES = (
    "自动化链路", "自动化流程", "媒体自动化", "任务链路", "自动化任务",
)
_AUTOMATION_DIAGNOSIS_INTENTS = (
    "诊断", "检查", "查看", "状态", "健康", "异常", "失败", "怎么样", "是否正常",
)
_AUTOMATION_DIAGNOSIS_REJECT_TOKENS = (
    "配置", "设置", "开启", "关闭", "启用", "停用", "立即运行", "运行一次", "执行一次",
)
_AUTOMATION_SPECIFIC_SCOPES = (
    "strm", "云盘整理", "光鸭整理", "整理任务",
    "rss", "订阅源",
    "下载队列", "下载任务", "下载自动化", "qbittorrent", "qb 下载", "qb下载", "下载器",
    "本地媒体", "本地整理", "本地入库",
)


def is_automation_pipeline_diagnosis_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if any(token in normalized for token in _AUTOMATION_DIAGNOSIS_REJECT_TOKENS):
        return False
    if any(scope in normalized for scope in _AUTOMATION_SPECIFIC_SCOPES):
        return False
    return any(scope in normalized for scope in _AUTOMATION_DIAGNOSIS_SCOPES) and any(
        intent in normalized for intent in _AUTOMATION_DIAGNOSIS_INTENTS
    )


_DIAGNOSTIC_READ_INTENTS = (
    ReadIntentSpec("indexer.diagnose_readiness", is_indexer_readiness_diagnosis_message),
    ReadIntentSpec("downloads.diagnose_queue", is_download_queue_diagnosis_message),
    ReadIntentSpec("rss.diagnose", is_rss_diagnosis_message),
    ReadIntentSpec("local_media.diagnose", local_media_intents.is_local_media_diagnosis_message),
    ReadIntentSpec("automation.diagnose_pipeline", is_automation_pipeline_diagnosis_message),
    # 工作区健康检查范围最宽，必须保持在所有专项诊断之后。
    ReadIntentSpec("workspace.health", is_workspace_health_message),
)


_FEATURE_ALIASES = (
    ("web_search", ("通用网页搜索", "联网搜索", "网页搜索", "web search", "tavily 搜索", "tavily搜索")),
    ("resource_results", ("探索页站点资源结果", "站点资源结果", "探索资源结果", "资源结果显示")),
    ("indexer_search", (
        "多站资源搜索", "多站资源索引", "多站资源索引总开关", "资源搜索总开关",
        "多站索引", "资源站搜索", "索引站搜索", "索引搜索",
    )),
    ("douban", ("豆瓣探索", "豆瓣电影探索", "豆瓣剧集探索")),
    ("discovery", ("媒体探索", "探索功能", "探索服务")),
    ("offline_magnet", (
        "光鸭磁力链接离线转存", "光鸭磁力离线转存", "磁力链接离线转存", "磁力离线转存", "磁力链接转存",
    )),
    ("offline_ed2k", (
        "光鸭 ed2k 离线转存", "ed2k 离线转存", "ed2k 链接离线转存", "ed2k 链接转存",
    )),
    ("offline_http", (
        "光鸭 http 离线转存", "http 离线转存", "http 链接离线转存", "http 链接转存",
    )),
    ("strm_metadata", (
        "strm 伴随元数据同步", "strm 元数据同步", "strm 伴随元数据", "同步伴随元数据",
    )),
    ("download_verification_notify", (
        "下载后入库复核通知", "下载复核通知", "入库复核通知", "任务复核通知",
    )),
)
_FEATURE_ENABLE_VERBS = {"开启", "启用", "打开"}
_FEATURE_ACTION_PATTERN = r"开启|启用|打开|关闭|关掉|停用|禁用"
_FEATURE_ACTION_PREFIX = r"(?:请(?:帮我)?|帮我|麻烦(?:帮我)?)?\s*"
_FEATURE_ACTION_SUFFIX = r"(?:\s*一下)?\s*[。.!！]*"
_FEATURE_ACTION_REJECT_TOKENS = (
    "不想", "不要", "别", "如果", "假如", "要是",
    "状态", "是否", "为什么", "怎么", "如何", "能否", "有没有",
    "会怎样", "有什么风险", "还能", "看看", "没打开", "未开启", "未关闭",
    "吗", "呢", "？", "?",
)
_FEATURE_SUMMARY_ALIASES = tuple(
    alias
    for _feature, aliases in _FEATURE_ALIASES
    for alias in aliases
) + ("功能开关", "功能状态", "探索配置")
_FEATURE_SUMMARY_TOGGLE_TOKENS = (
    "开启", "启用", "关闭", "停用", "禁用", "打开", "开着", "关着", "生效", "功能开关",
)
_FEATURE_SUMMARY_STATUS_PATTERN = re.compile(
    r"(?:状态|健康(?:吗|呢|如何|怎么样|？|\?|$)|"
    r"(?:是否|有没有|有无|已经|当前|现在)?\s*(?:已)?(?:开启|启用|关闭|停用|禁用|打开|开着|关着|生效)|"
    r"(?:是否|当前|现在)?\s*(?:可用|能用|正常)(?:吗|呢|如何|怎么样|？|\?|$)|"
    r"为什么.*(?:不可用|不能用|没打开|未开启|未启用|未生效))"
)
_WEB_SEARCH_FEATURE_STATUS_PATTERN = re.compile(
    r"^(?:请(?:帮我)?|帮我|麻烦(?:帮我)?|查看|检查|告诉我)?\s*"
    r"(?:通用网页搜索|联网搜索|网页搜索|web search|tavily\s*搜索)\s*(?:功能)?\s*"
    r"(?:现在|当前|已经)?\s*"
    r"(?:是什么状态|状态(?:如何|怎么样|正常吗|正常么|吗|呢)?|"
    r"是否(?:已)?(?:开启|启用|关闭|停用|禁用|打开|生效|可用|能用|正常)|"
    r"有没有(?:开启|启用|关闭|停用|打开|生效)|"
    r"有无(?:开启|启用|关闭|停用|打开|生效)|"
    r"(?:已)?(?:开启|启用|关闭|停用|禁用|打开|生效|可用|能用|正常)(?:吗|呢|？|\?)?|"
    r"为什么(?:不可用|不能用|没打开|未开启|未启用|未生效))"
    r"\s*[。.!！？?]*$"
)


def feature_state_request(message: str) -> dict[str, Any] | None:
    """只接受单一、肯定、完整的功能开关命令，避免把讨论或导航误判为写操作。"""
    normalized = str(message or "").casefold().strip()
    if not normalized or any(token in normalized for token in _FEATURE_ACTION_REJECT_TOKENS):
        return None
    action_matches = re.findall(_FEATURE_ACTION_PATTERN, normalized)
    if len(action_matches) != 1:
        return None

    for feature, aliases in _FEATURE_ALIASES:
        for alias in aliases:
            escaped_alias = re.escape(alias)
            verb_first = re.fullmatch(
                _FEATURE_ACTION_PREFIX
                + rf"(?:(?:把|将)\s*)?(?P<verb>{_FEATURE_ACTION_PATTERN})\s*(?:一下\s*)?{escaped_alias}"
                + _FEATURE_ACTION_SUFFIX,
                normalized,
            )
            target_first = re.fullmatch(
                _FEATURE_ACTION_PREFIX
                + rf"(?:把|将)\s*{escaped_alias}\s*(?:给我\s*)?(?P<verb>{_FEATURE_ACTION_PATTERN})"
                + _FEATURE_ACTION_SUFFIX,
                normalized,
            )
            matched = verb_first or target_first
            if matched is not None:
                return {
                    "feature": feature,
                    "enabled": matched.group("verb") in _FEATURE_ENABLE_VERBS,
                }
    return None


_FEATURE_REFERENCE_PATTERNS = (
    re.compile(
        _FEATURE_ACTION_PREFIX
        + rf"(?:(?:把|将)\s*)?(?P<verb>{_FEATURE_ACTION_PATTERN})\s*(?:一下\s*)?"
        + r"(?:它|这个(?:功能|开关)?|该(?:功能|开关))"
        + _FEATURE_ACTION_SUFFIX
    ),
    re.compile(
        _FEATURE_ACTION_PREFIX
        + r"(?:把|将)\s*(?:它|这个(?:功能|开关)?|该(?:功能|开关))\s*(?:给我\s*)?"
        + rf"(?P<verb>{_FEATURE_ACTION_PATTERN})"
        + _FEATURE_ACTION_SUFFIX
    ),
)
_FEATURE_CORRECTION_PATTERN = re.compile(
    r"^(?:(?:我|刚才|前面)\s*)?(?:(?:说|讲|弄|搞)\s*)?反了"
    r"[，,、\s]*(?:(?:还是|应该|改成|改为)\s*(?:是\s*)?)?"
    rf"(?P<verb>{_FEATURE_ACTION_PATTERN})"
    r"(?:\s*(?:它|这个(?:功能|开关)?|该(?:功能|开关)))?"
    r"(?:\s*一下)?\s*[。.!！]*$"
)


def _latest_single_feature_topic(
    conversation_context: list[dict[str, Any]] | None,
) -> str | None:
    """只信任最近一条用户消息中的单一功能主题，避免跨话题误绑定。"""
    for item in reversed(conversation_context or []):
        if not isinstance(item, dict) or str(item.get("role") or "").casefold() != "user":
            continue
        context_text = unicodedata.normalize(
            "NFKC", str(item.get("text") or "")
        ).casefold().strip()
        if not context_text:
            return None
        mentioned = {
            feature
            for feature, aliases in _FEATURE_ALIASES
            if any(alias in context_text for alias in aliases)
        }
        return mentioned.pop() if len(mentioned) == 1 else None
    return None


def feature_state_followup_request(
    message: str,
    conversation_context: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """解析明确的功能代词续句与“说反了”纠正，仍只生成待确认写操作。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if not normalized:
        return None

    correction = _FEATURE_CORRECTION_PATTERN.fullmatch(normalized)
    if correction is not None:
        feature = _latest_single_feature_topic(conversation_context)
        if feature is None:
            return None
        return {
            "feature": feature,
            "enabled": correction.group("verb") in _FEATURE_ENABLE_VERBS,
        }

    if any(token in normalized for token in _FEATURE_ACTION_REJECT_TOKENS):
        return None
    matched = next(
        (candidate for pattern in _FEATURE_REFERENCE_PATTERNS if (candidate := pattern.fullmatch(normalized))),
        None,
    )
    if matched is None:
        return None
    feature = _latest_single_feature_topic(conversation_context)
    if feature is None:
        return None
    return {
        "feature": feature,
        "enabled": matched.group("verb") in _FEATURE_ENABLE_VERBS,
    }


_INDEXER_SITE_ALIASES = (
    ("animetosho", ("anime tosho", "animetosho")),
    ("tpb", ("the pirate bay", "pirate bay", "海盗湾", "tpb")),
    ("btbtla", ("btbtla",)),
    ("sukebei", ("sukebei",)),
    ("mikan", ("mikan", "蜜柑")),
    ("nyaa", ("nyaa",)),
    ("1lou", ("1lou", "1楼", "一楼")),
)
_INDEXER_SITE_LIST_SEPARATOR = re.compile(r"^(?:\s+|[,，、;+；/]|和|与|及)+")
_INDEXER_SITE_SET_PATTERNS = (
    re.compile(
        r"^(?:请(?:帮我)?|帮我|麻烦(?:帮我)?)?\s*(?:把|将)?\s*"
        r"(?:(?:参与(?:多站)?资源检索|多站资源搜索|资源站搜索|索引站搜索|索引搜索)的?)?"
        r"(?:资源站点|站点)(?:选择)?\s*(?:设置为|设定为|设为|调整为|配置为|改为|改成)\s*"
        r"(?P<sites>.+?)\s*[。.!！]*$"
    ),
    re.compile(
        r"^(?:请(?:帮我)?|帮我|麻烦(?:帮我)?)?\s*(?:设置|设定|配置|调整)\s*"
        r"(?:(?:参与(?:多站)?资源检索|多站资源搜索|资源站搜索|索引站搜索|索引搜索)的?)?"
        r"(?:资源站点|站点)(?:选择)?\s*为\s*(?P<sites>.+?)\s*[。.!！]*$"
    ),
    re.compile(
        r"^(?:请(?:帮我)?|帮我|麻烦(?:帮我)?)?\s*只\s*(?:启用|开启|打开|开)\s*"
        r"(?P<sites>.+?)\s*(?:这些)?(?:资源站点|站点)\s*[。.!！]*$"
    ),
    re.compile(
        r"^(?:请(?:帮我)?|帮我|麻烦(?:帮我)?)?\s*(?:把|将)?\s*"
        r"(?:资源站点|资源站|站点)\s*(?:仅|只)?\s*保留(?:为)?\s*"
        r"(?P<sites>.+?)\s*[。.!！]*$"
    ),
)
_INDEXER_SITE_SET_REJECT_TOKENS = (
    "不想", "不要", "别", "如果", "假如", "要是", "是否", "为什么", "怎么", "如何",
    "能否", "会怎样", "有什么风险", "吗", "呢", "？", "?", "并且", "然后", "同时",
)
_INDEXER_SITE_MUTATION_ACTION_PATTERN = (
    r"添加|增加|加上|启用|开启|打开|移除|删除|去掉|关闭|关掉|停用|禁用"
)
_INDEXER_SITE_ADD_VERBS = {"添加", "增加", "加上", "启用", "开启", "打开"}
_INDEXER_SITE_MUTATION_PATTERNS = (
    re.compile(
        _FEATURE_ACTION_PREFIX
        + r"(?P<only>只\s*)?"
        + rf"(?P<verb>{_INDEXER_SITE_MUTATION_ACTION_PATTERN})\s*(?:一下\s*)?"
        + r"(?P<sites>.+?)"
        + _FEATURE_ACTION_SUFFIX
    ),
    re.compile(
        _FEATURE_ACTION_PREFIX
        + rf"(?:把|将)\s*(?P<sites>.+?)\s*(?:给我\s*)?"
        + rf"(?P<verb>{_INDEXER_SITE_MUTATION_ACTION_PATTERN})"
        + _FEATURE_ACTION_SUFFIX
    ),
)
_INDEXER_SITE_MUTATION_REJECT_TOKENS = (
    "不想", "不要", "别", "如果", "假如", "要是", "是否", "为什么", "怎么", "如何",
    "能否", "会怎样", "有什么风险", "吗", "呢", "？", "?", "并且", "然后", "同时",
)
_INDEXER_SITE_ORDINARY_QUALIFIER_PATTERN = re.compile(
    r"(?:普通(?:资源站点|资源站|站点)|非成人(?:资源站点|资源站|站点)|"
    r"(?:不含|不包括|不包含|排除|除了)\s*sukebei)"
)
_INDEXER_SITE_SENSITIVE_QUALIFIER_PATTERN = re.compile(
    r"(?:(?<!不)(?:包括|包含|含)|连|以及)\s*(?:sukebei|成人(?:资源站点|资源站|站点))"
)
_INDEXER_SITE_RETAIN_PATTERNS = (
    re.compile(
        r"^(?:请(?:帮我)?|帮我|麻烦(?:帮我)?)?\s*(?:把|将)?\s*"
        r"(?:所有|全部)\s*(?:资源站点|资源站|站点)\s*(?:都)?\s*"
        r"(?:关闭|关掉|停用|禁用|移除|删除|去掉)\s*"
        r"(?:但|但是|只)?\s*(?:保留|留下)\s*(?P<sites>.+?)\s*[。.!！]*$"
    ),
    re.compile(
        r"^(?:请(?:帮我)?|帮我|麻烦(?:帮我)?)?\s*(?:把|将)?\s*"
        r"(?:关闭|关掉|停用|禁用|移除|删除|去掉)\s*"
        r"(?:所有|全部)\s*(?:资源站点|资源站|站点)\s*(?:都)?\s*"
        r"(?:但|但是|只)?\s*(?:保留|留下)\s*(?P<sites>.+?)\s*[。.!！]*$"
    ),
    re.compile(
        r"^(?:请(?:帮我)?|帮我|麻烦(?:帮我)?)?\s*"
        r"(?:关闭|关掉|停用|禁用|移除|删除|去掉)\s*"
        r"(?:除|除了)\s*(?P<sites>.+?)\s*(?:外|以外)\s*(?:的)?\s*"
        r"(?:所有|全部)\s*(?:资源站点|资源站|站点)\s*[。.!！]*$"
    ),
)
def _normalize_indexer_command(value: str) -> str:
    """归一化资源站点命令中的常见口语和输入法误差。"""
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return normalized.replace("开大", "打开").replace("启开", "开启")


_INDEXER_SITE_SUMMARY_PATTERN = re.compile(
    r"^(?:请(?:帮我)?|帮我|麻烦(?:帮我)?|查看|告诉我|列出|列一下|显示)?\s*"
    r"(?:当前|现在|全部|所有)?\s*(?:启用|开启|配置|选择|使用|用了)?\s*"
    r"(?:了)?\s*(?:哪些|什么)?\s*(?:参与)?(?:多站资源搜索的?)?(?:资源|索引)站点"
    r"(?:有哪些|是什么|配置|选择|状态|吗|呢)?\s*[。.!！？?]*$"
    r"|^(?:请(?:帮我)?|帮我|麻烦(?:帮我)?|查看|告诉我|列出|列一下|显示)?\s*"
    r"(?:当前|现在|全部|所有)?\s*(?:使用|启用|开启|配置|选择|用了)的?\s*(?:资源|索引)站点"
    r"(?:有哪些|是什么|配置|选择|状态|吗|呢)?\s*[。.!！？?]*$"
)


def _parse_indexer_site_list(value: str) -> list[str] | None:
    remaining = _normalize_indexer_command(value)
    selected: list[str] = []
    while remaining:
        separator = _INDEXER_SITE_LIST_SEPARATOR.match(remaining)
        if separator is not None:
            remaining = remaining[separator.end():]
            if not remaining:
                break
        match: tuple[str, str] | None = None
        for site_id, aliases in _INDEXER_SITE_ALIASES:
            for alias in aliases:
                if remaining.startswith(alias):
                    match = (site_id, alias)
                    break
            if match is not None:
                break
        if match is None:
            return None
        site_id, alias = match
        if site_id not in selected:
            selected.append(site_id)
        remaining = remaining[len(alias):]
    return selected or None


def indexer_sites_request(message: str) -> dict[str, Any] | None:
    """仅识别明确、单一的固定资源站点设置命令。"""
    normalized = _normalize_indexer_command(message)
    if not normalized or any(token in normalized for token in _INDEXER_SITE_SET_REJECT_TOKENS):
        return None
    for pattern in _INDEXER_SITE_SET_PATTERNS:
        matched = pattern.fullmatch(normalized)
        if matched is None:
            continue
        site_ids = _parse_indexer_site_list(matched.group("sites"))
        if site_ids is None:
            raise AgentToolError("包含未知或格式无效的资源站点")
        return {"site_ids": site_ids}
    return None


def _parse_indexer_site_expression(value: str) -> list[str] | None:
    """解析增删命令中的站点表达式；仅容忍无语义的尾部量词。"""
    normalized = _normalize_indexer_command(value)
    normalized = re.sub(
        r"\s*(?:这些)?(?:资源站点|资源站|站点)\s*$",
        "",
        normalized,
    ).strip()
    return _parse_indexer_site_list(normalized)


def indexer_site_change_request(message: str) -> dict[str, Any] | None:
    """识别白名单站点的安全全选或增量增删，不推断成人站点。"""
    normalized = _normalize_indexer_command(message)
    if not normalized or any(
        token in normalized for token in _INDEXER_SITE_MUTATION_REJECT_TOKENS
    ):
        return None
    unopened_scope = bool(
        re.search(
            r"(?:开启|打开|启用).{0,12}(?:未开启|未打开|未启用).{0,12}(?:所有|全部)?(?:资源站点|资源站|索引站点|站点)",
            normalized,
        )
        or re.search(
            r"(?:未开启|未打开|未启用).{0,12}(?:所有|全部)?(?:资源站点|资源站|索引站点|站点).{0,12}(?:开启|打开|启用)",
            normalized,
        )
    )
    if unopened_scope:
        # “开启未打开的站点”是增量动作，不能把当前已启用但不在默认集合中的
        # 显式站点（例如 Sukebei）一并关闭。
        return {"operation": "add", "site_ids": list(DEFAULT_INDEXER_SITE_IDS)}

    action_matches = re.findall(_INDEXER_SITE_MUTATION_ACTION_PATTERN, normalized)
    if len(action_matches) != 1:
        return None
    verb = action_matches[0]

    for pattern in _INDEXER_SITE_RETAIN_PATTERNS:
        retained = pattern.fullmatch(normalized)
        if retained is None:
            continue
        site_ids = _parse_indexer_site_expression(retained.group("sites"))
        if not site_ids:
            raise AgentToolError("保留的资源站点未知或格式无效")
        return {"operation": "replace", "site_ids": site_ids}

    ordinary_scope = bool(_INDEXER_SITE_ORDINARY_QUALIFIER_PATTERN.search(normalized))
    sensitive_scope = bool(_INDEXER_SITE_SENSITIVE_QUALIFIER_PATTERN.search(normalized))
    has_all_scope = bool(
        re.search(r"(?:所有|全部).{0,8}(?:资源站点|资源站|站点)", normalized)
        or ordinary_scope
    )
    if has_all_scope:
        if ordinary_scope and sensitive_scope:
            return {"operation": "clarify_scope_conflict", "site_ids": []}
        if verb not in _INDEXER_SITE_ADD_VERBS:
            # 返回空目标，让上层给出“关闭多站资源索引”的专用安全引导。
            return {"operation": "replace", "site_ids": []}
        if ordinary_scope:
            return {"operation": "replace", "site_ids": list(DEFAULT_INDEXER_SITE_IDS)}
        if sensitive_scope:
            return {"operation": "replace", "site_ids": list(INDEXER_SITE_ORDER)}
        # 未明确成人内容时采用普通站点全集；显式提及 Sukebei 才纳入成人站点。
        return {"operation": "replace", "site_ids": list(DEFAULT_INDEXER_SITE_IDS)}

    has_known_site = any(
        alias in normalized
        for _site_id, aliases in _INDEXER_SITE_ALIASES
        for alias in aliases
    )
    if not has_known_site:
        return None

    for pattern in _INDEXER_SITE_MUTATION_PATTERNS:
        matched = pattern.fullmatch(normalized)
        if matched is None:
            continue
        site_ids = _parse_indexer_site_expression(matched.group("sites"))
        if site_ids is None:
            if "资源站" in normalized or "站点" in normalized:
                raise AgentToolError("包含未知或格式无效的资源站点")
            return None
        if matched.groupdict().get("only") and matched.group("verb") in _INDEXER_SITE_ADD_VERBS:
            operation = "replace"
        else:
            operation = "add" if matched.group("verb") in _INDEXER_SITE_ADD_VERBS else "remove"
        return {
            "operation": operation,
            "site_ids": site_ids,
        }
    return None


def resolve_indexer_site_change(
    change: dict[str, Any],
    *,
    current_site_ids: tuple[str, ...] | list[str] | None = None,
) -> list[str]:
    """把增量命令解析为完整、有序的目标集合，供现有原子确认工具使用。"""
    operation = str(change.get("operation") or "")
    requested = {
        str(site_id)
        for site_id in change.get("site_ids", [])
        if str(site_id) in INDEXER_SITE_ORDER
    }
    if operation == "replace":
        selected = requested
    elif operation in {"add", "remove"}:
        selected = set(
            current_indexer_site_ids(strict=True)
            if current_site_ids is None
            else current_site_ids
        )
        if operation == "add":
            selected.update(requested)
        else:
            selected.difference_update(requested)
    else:
        raise AgentToolError("不支持的资源站点变更操作")
    return [site_id for site_id in INDEXER_SITE_ORDER if site_id in selected]


def is_indexer_sites_summary_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    return bool(normalized and _INDEXER_SITE_SUMMARY_PATTERN.fullmatch(normalized))


def is_feature_state_message(message: str) -> bool:
    return feature_state_request(message) is not None


def is_feature_summary_message(message: str) -> bool:
    """识别功能状态问句；显式开关命令始终留给确认写入流程。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if not normalized or feature_state_request(normalized) is not None:
        return False
    if re.search(r"(?:找|搜|搜索|查找)\s*[《「『\"']", normalized):
        return False
    if not any(alias in normalized for alias in _FEATURE_SUMMARY_ALIASES):
        return False
    if not _FEATURE_SUMMARY_STATUS_PATTERN.search(normalized):
        return False
    if is_web_search_message(normalized) and not _WEB_SEARCH_FEATURE_STATUS_PATTERN.fullmatch(normalized):
        return False
    if (
        is_indexer_resource_search_message(normalized)
        or is_discovery_search_message(normalized)
    ) and not any(token in normalized for token in _FEATURE_SUMMARY_TOGGLE_TOKENS):
        return False
    return True



_TELEGRAM_TEST_NOTIFICATION_PATTERNS = (
    re.compile(
        r"^(?:请(?:帮我)?\s*)?(?:测试|检查)\s*(?:一下\s*)?"
        r"(?:telegram|tg)\s*(?:通知|消息|通知通道|消息通道)(?:\s*连接)?(?:\s*测试)?"
        r"(?:一下|一次)?[.!。！？]?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:请(?:帮我)?\s*)?(?:发送|发|推送)\s*(?:一条\s*)?"
        r"(?:telegram|tg)\s*(?:连接\s*)?测试(?:通知|消息)"
        r"(?:一下|一次)?[.!。！？]?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:请(?:帮我)?\s*)?给\s*(?:telegram|tg)\s*"
        r"(?:发送|发|推送)\s*(?:一条\s*)?(?:连接\s*)?测试(?:通知|消息)"
        r"(?:一下|一次)?[.!。！？]?$",
        re.IGNORECASE,
    ),
)


def is_telegram_test_notification_message(message: str) -> bool:
    """只接受固定测试通知意图，拒绝任意消息正文和复合操作。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if not normalized or any(token in normalized for token in (
        "不要", "别", "如果", "假如", "能否", "是否", "为什么", "怎么",
        "同时", "顺便", "以及", "然后", "内容", "正文", "发给", "发送给",
    )):
        return False
    return any(pattern.fullmatch(normalized) for pattern in _TELEGRAM_TEST_NOTIFICATION_PATTERNS)


_SAFE_POLICY_TARGETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("tmdb_match_mode", ("tmdb 匹配模式", "tmdb匹配模式", "tmdb 匹配", "tmdb匹配")),
    ("login_wallpaper_mode", ("登录页壁纸模式", "登录页壁纸", "登录壁纸")),
    ("web_search_max_results", (
        "网页搜索单次结果上限", "网页搜索结果上限", "网页搜索每次最多返回",
        "联网搜索结果上限", "tavily 结果上限", "tavily结果上限",
    )),
    ("web_search_timeout_seconds", (
        "网页搜索请求超时", "网页搜索超时", "联网搜索超时",
        "tavily 请求超时", "tavily请求超时",
    )),
    ("web_search_cache_ttl_seconds", (
        "网页搜索缓存时间", "网页搜索缓存 ttl", "网页搜索缓存ttl",
        "联网搜索缓存时间", "tavily 缓存时间", "tavily缓存时间",
    )),
    ("web_search_daily_credit_limit", (
        "网页搜索每日额度", "网页搜索每天额度", "联网搜索每日额度",
        "tavily 每日额度", "tavily每日额度", "tavily 每天额度", "tavily每天额度",
    )),
    ("discovery_stale_ttl_seconds", (
        "媒体探索旧缓存保留时间", "探索旧缓存保留时间", "探索旧缓存 ttl",
        "探索旧缓存ttl", "探索备用缓存时间",
    )),
    ("discovery_cache_ttl_seconds", (
        "媒体探索缓存时间", "媒体探索缓存 ttl", "媒体探索缓存ttl",
        "探索缓存时间", "探索缓存 ttl", "探索缓存ttl",
    )),
    ("douban_cache_ttl_seconds", (
        "豆瓣探索缓存时间", "豆瓣探索缓存 ttl", "豆瓣探索缓存ttl",
        "豆瓣缓存时间", "豆瓣缓存 ttl", "豆瓣缓存ttl",
    )),
    ("web_search_depth", ("网页搜索深度", "联网搜索深度", "tavily 搜索深度", "tavily搜索深度")),
    ("indexer_btbtla_min_interval_seconds", (
        "btbtla 最小请求间隔", "btbtla最小请求间隔", "btbtla 请求间隔", "btbtla请求间隔",
    )),
)
_SAFE_POLICY_CHOICE_VALUES: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "tmdb_match_mode": (
        ("strict", ("严格", "严格模式", "strict")),
        ("loose", ("宽松", "宽松模式", "loose")),
    ),
    "login_wallpaper_mode": (
        ("default", ("默认", "默认壁纸", "default")),
        ("tmdb", ("tmdb", "tmdb 每日电影", "tmdb每日电影", "每日电影")),
    ),
    "web_search_depth": (
        ("basic", ("基础", "基础模式", "basic")),
        ("advanced", ("高级", "高级模式", "advanced")),
    ),
}
_SAFE_POLICY_MUTATION_VERBS = ("设为", "设置为", "设成", "改为", "改成", "调整为", "调整到")
_SAFE_POLICY_REJECT_TOKENS = (
    "不要", "别", "不想", "无需", "如果", "假如", "要是", "能否", "是否",
    "为什么", "怎么", "如何", "风险", "影响", "吗", "呢", "？", "?",
    "同时", "顺便", "以及", "然后",
)
_SAFE_POLICY_SUMMARY_INTENTS = ("查看", "检查", "列出", "显示", "当前", "现在", "告诉我")
_SAFE_POLICY_SUMMARY_SCOPES = (
    "安全策略", "agent 可管理策略", "agent可管理策略", "agent 策略", "agent策略",
    "网页搜索策略", "tmdb 匹配策略", "tmdb匹配策略", "登录页壁纸策略",
    "btbtla 请求策略", "btbtla请求策略", "探索缓存策略", "豆瓣缓存策略",
)


def _safe_policy_targets_in_message(message: str) -> list[str]:
    matches: list[tuple[str, int, int]] = []
    for policy, aliases in _SAFE_POLICY_TARGETS:
        for alias in aliases:
            start = message.find(alias)
            while start >= 0:
                matches.append((policy, start, start + len(alias)))
                start = message.find(alias, start + 1)
    policies: list[str] = []
    for policy, start, end in matches:
        # “豆瓣探索缓存时间”同时包含较短的“探索缓存时间”。只忽略同一文本片段
        # 内被更具体目标完整覆盖的短别名；分处不同位置的两个目标仍会被识别为复合命令。
        if any(
            other_policy != policy
            and other_start <= start
            and other_end >= end
            and (other_end - other_start) > (end - start)
            for other_policy, other_start, other_end in matches
        ):
            continue
        if policy not in policies:
            policies.append(policy)
    return policies


def safe_policy_request(message: str) -> dict[str, Any] | None:
    """识别单一、明确的白名单策略赋值；讨论、问句和复合命令一律不写。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if not normalized or any(token in normalized for token in _SAFE_POLICY_REJECT_TOKENS):
        return None
    targets = _safe_policy_targets_in_message(normalized)
    if len(targets) != 1:
        return None
    policy = targets[0]
    if sum(normalized.count(verb) for verb in _SAFE_POLICY_MUTATION_VERBS) != 1:
        return None

    target_aliases = next(aliases for key, aliases in _SAFE_POLICY_TARGETS if key == policy)
    target_pattern = "(?:" + "|".join(re.escape(alias) for alias in target_aliases) + ")"
    verb_pattern = "(?:" + "|".join(_SAFE_POLICY_MUTATION_VERBS) + ")"
    prefix = r"(?:请(?:帮我)?|帮我|麻烦(?:帮我)?)?\s*(?:把|将)?\s*"
    suffix = r"\s*(?:一下)?\s*[。.!！]*"

    if policy in _SAFE_POLICY_CHOICE_VALUES:
        values = _SAFE_POLICY_CHOICE_VALUES[policy]
        value_pattern = "(?:" + "|".join(
            re.escape(alias) for _value, aliases in values for alias in aliases
        ) + ")"
        matched = re.fullmatch(
            prefix + target_pattern + r"\s*" + verb_pattern
            + r"\s*(?P<value>" + value_pattern + ")" + suffix,
            normalized,
        )
        if matched is None:
            return None
        raw_value = matched.group("value")
        for value, aliases in values:
            if raw_value in aliases:
                return {"policy": policy, "value": value}
        return None

    if policy == "web_search_max_results":
        unit_pattern = r"条?"
    elif policy == "web_search_daily_credit_limit":
        unit_pattern = r"(?:次|额度)?"
    elif policy in {
        "web_search_cache_ttl_seconds",
        "discovery_cache_ttl_seconds",
        "discovery_stale_ttl_seconds",
        "douban_cache_ttl_seconds",
    }:
        unit_pattern = r"(?P<unit>秒|分钟|小时)?"
    else:
        unit_pattern = r"秒?"
    matched = re.fullmatch(
        prefix + target_pattern + r"\s*" + verb_pattern
        + r"\s*(?P<value>\d{1,7})\s*" + unit_pattern + suffix,
        normalized,
    )
    if matched is None:
        return None
    value = int(matched.group("value"))
    if "unit" in matched.groupdict():
        multiplier = {"分钟": 60, "小时": 3600}.get(matched.group("unit") or "秒", 1)
        value *= multiplier
    return {"policy": policy, "value": value}


def is_safe_policy_summary_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if not normalized or safe_policy_request(normalized) is not None:
        return False
    if any(verb in normalized for verb in _SAFE_POLICY_MUTATION_VERBS):
        return False
    return (
        any(intent in normalized for intent in _SAFE_POLICY_SUMMARY_INTENTS)
        and (
            any(scope in normalized for scope in _SAFE_POLICY_SUMMARY_SCOPES)
            or bool(_safe_policy_targets_in_message(normalized))
        )
    )


def is_safe_policy_mutation_candidate(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    return bool(
        normalized
        and _safe_policy_targets_in_message(normalized)
        and any(verb in normalized for verb in _SAFE_POLICY_MUTATION_VERBS)
        and safe_policy_request(normalized) is None
    )

_CONFIG_COMPONENT_ALIASES = (
    ("resource_results", ("探索页站点资源结果", "站点资源结果", "探索资源结果", "资源结果显示")),
    ("indexer_search", ("多站资源搜索", "多站资源索引", "多站索引", "资源站搜索", "索引站搜索", "索引搜索")),
    ("ai_recognition", ("ai 识别回退", "ai识别回退", "ai 识别", "ai识别")),
    ("emby", ("jellyfin 10.x", "jellyfin10.x", "jellyfin 10", "emby")),
    ("jellyfin", ("jellyfin 12", "jellyfin")),
    ("qbittorrent", ("qbittorrent", "qbit", "qb 下载器", "qb下载器")),
    ("tmdb", ("tmdb",)),
    ("strm", ("strm",)),
    ("douban", ("豆瓣探索", "豆瓣电影探索", "豆瓣剧集探索")),
    ("discovery", ("媒体探索", "探索功能", "探索服务")),
)
_CONFIG_EXPLAIN_INTENTS = (
    "为什么", "怎么配置", "如何配置", "配置说明", "配置要求",
    "需要什么", "需要哪些", "缺少什么", "缺什么", "配置不完整",
    "未配置", "不可用", "不能用", "用不了",
)


def config_component_explain_request(message: str) -> dict[str, str] | None:
    """识别单一白名单组件的解释型问句，不截获测试、诊断或写操作。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if not normalized or re.search(_FEATURE_ACTION_PATTERN, normalized):
        return None
    if any(token in normalized for token in (
        "测试", "校验", "验证", "诊断", "连通", "健康检查",
    )):
        return None
    if not any(intent in normalized for intent in _CONFIG_EXPLAIN_INTENTS):
        return None

    matched = {
        component
        for component, aliases in _CONFIG_COMPONENT_ALIASES
        if any(alias in normalized for alias in aliases)
    }
    if "emby" in matched and any(
        alias in normalized for alias in ("jellyfin 10.x", "jellyfin10.x", "jellyfin 10")
    ):
        matched.discard("jellyfin")
    if len(matched) != 1:
        return None
    return {"component": next(iter(matched))}


def media_server_test_type(message: str) -> str:
    normalized = str(message or "").casefold()
    if "strm" in normalized:
        return ""
    has_intent = any(token in normalized for token in _MEDIA_SERVER_TEST_VERBS) and any(
        token in normalized for token in _MEDIA_SERVER_TEST_SCOPES
    )
    if not has_intent:
        return ""
    has_jellyfin = "jellyfin" in normalized
    has_emby = "emby" in normalized
    if has_jellyfin == has_emby:
        return ""
    return "jellyfin" if has_jellyfin else "emby"


def is_media_server_test_message(message: str) -> bool:
    return bool(media_server_test_type(message))


def is_media_server_diagnosis_message(message: str) -> bool:
    """识别媒体服务器汇总、版本与兼容槽位诊断，保留单节点连接测试边界。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if not normalized or any(token in normalized for token in _MEDIA_SERVER_DIAGNOSIS_REJECT_TOKENS):
        return False
    if not any(scope in normalized for scope in _MEDIA_SERVER_DIAGNOSIS_SCOPES):
        return False
    if not any(intent in normalized for intent in _MEDIA_SERVER_DIAGNOSIS_INTENTS):
        return False
    has_jellyfin = "jellyfin" in normalized
    has_emby = "emby" in normalized
    if has_jellyfin != has_emby and not any(
        marker in normalized for marker in _MEDIA_SERVER_COMPATIBILITY_MARKERS
    ):
        return False
    return True


_RECOGNITION_RULE_SCOPES = (
    ("preprocess_rule", ("识别预处理规则", "预处理规则")),
    (
        "tmdb_regex_rule",
        (
            "tmdb 正则规则",
            "tmdb正则规则",
            "tmdb 强制匹配规则",
            "tmdb强制匹配规则",
            "强制匹配规则",
        ),
    ),
    ("knowledge_entry", ("识别知识条目", "知识条目", "识别知识")),
)
_RECOGNITION_RULE_ENABLE_INTENTS = ("启用", "开启", "打开")
_RECOGNITION_RULE_DISABLE_INTENTS = ("停用", "禁用", "关闭", "关掉")
_RECOGNITION_RULE_REJECT_TOKENS = (
    "全部", "所有", "每个", "批量",
    "创建", "新增", "删除", "移除", "修改", "编辑",
    "优先级", "别名", "映射", "表达式", "pattern", "内容",
)


def _recognition_rule_types(message: str) -> set[str]:
    normalized = _normalize_intent_message(message)
    return {
        rule_type
        for rule_type, aliases in _RECOGNITION_RULE_SCOPES
        if any(alias in normalized for alias in aliases)
    }


def recognition_rule_enabled_request(message: str) -> dict[str, Any] | None:
    """提取一条识别规则的精确启停请求；绝不猜测类型、编号或批量范围。"""
    normalized = _normalize_intent_message(message)
    # “识别”自身包含“别”，不能被通用否定词检测误判。仅移除该业务词后检测。
    discussion_text = normalized.replace("识别", "")
    if _is_dangerous_action_discussion(discussion_text):
        return None
    if any(token in normalized for token in _RECOGNITION_RULE_REJECT_TOKENS):
        return None
    rule_types = _recognition_rule_types(normalized)
    if len(rule_types) != 1:
        return None
    wants_enable = any(
        token in normalized for token in _RECOGNITION_RULE_ENABLE_INTENTS
    )
    wants_disable = any(
        token in normalized for token in _RECOGNITION_RULE_DISABLE_INTENTS
    )
    if wants_enable == wants_disable:
        return None
    rule_ids = {
        int(value)
        for value in re.findall(r"(?<!\d)(\d{1,10})(?!\d)", normalized)
        if 1 <= int(value) <= 2_147_483_647
    }
    if len(rule_ids) != 1:
        return None
    return {
        "rule_type": next(iter(rule_types)),
        "rule_id": next(iter(rule_ids)),
        "enabled": wants_enable,
    }


def is_recognition_rule_control_message(message: str) -> bool:
    """识别启停意图，用于缺少/冲突目标时返回澄清，而不是交给模型猜测。"""
    normalized = _normalize_intent_message(message)
    if not _recognition_rule_types(normalized):
        return False
    discussion_text = normalized.replace("识别", "")
    if _is_dangerous_action_discussion(discussion_text):
        return False
    if any(token in normalized for token in _RECOGNITION_RULE_REJECT_TOKENS):
        return False
    wants_enable = any(
        token in normalized for token in _RECOGNITION_RULE_ENABLE_INTENTS
    )
    wants_disable = any(
        token in normalized for token in _RECOGNITION_RULE_DISABLE_INTENTS
    )
    return wants_enable != wants_disable


_MEDIA_PROXY_SCOPES = ("媒体反代", "媒体代理", "media proxy")
_MEDIA_PROXY_ENABLE_INTENTS = ("启用", "开启", "打开")
_MEDIA_PROXY_DISABLE_INTENTS = ("停用", "禁用", "关闭", "关掉")
_MEDIA_PROXY_TEST_INTENTS = ("测试", "探测", "连通", "连接", "能用吗")
_MEDIA_PROXY_STATUS_INTENTS = (
    "状态", "概览", "汇总", "摘要", "列表", "运行情况", "有几个", "多少个",
)
_MEDIA_PROXY_EDIT_REJECT_TOKENS = (
    "创建", "新增", "删除", "移除", "修改", "编辑", "地址", "端口", "路径", "密钥",
    "token", "api key", "apikey", "strm",
)


def _has_media_proxy_scope(message: str) -> bool:
    normalized = _normalize_intent_message(message)
    return any(scope in normalized for scope in _MEDIA_PROXY_SCOPES)


def _media_proxy_instance_numbers(message: str) -> list[int]:
    """仅提取紧邻“媒体反代/媒体代理”的公开实例序号。"""
    normalized = _normalize_intent_message(message)
    scope_pattern = r"(?:媒体反代|媒体代理|media\s+proxy)"
    patterns = (
        rf"{scope_pattern}\s*(?:实例)?\s*(?:第\s*)?(\d{{1,4}})\s*(?:号|个)?",
        rf"第\s*(\d{{1,4}})\s*(?:个|号)?\s*{scope_pattern}\s*(?:实例)?",
        rf"(\d{{1,4}})\s*号\s*{scope_pattern}\s*(?:实例)?",
    )
    found: set[int] = set()
    for pattern in patterns:
        for matched in re.finditer(pattern, normalized):
            number = int(matched.group(1))
            if 1 <= number <= 10_000:
                found.add(number)
    return sorted(found)


def media_proxy_instance_enabled_request(message: str) -> dict[str, Any] | None:
    normalized = _normalize_intent_message(message)
    if not _has_media_proxy_scope(normalized) or _is_dangerous_action_discussion(normalized):
        return None
    if any(token in normalized for token in _MEDIA_PROXY_EDIT_REJECT_TOKENS):
        return None
    wants_enable = any(token in normalized for token in _MEDIA_PROXY_ENABLE_INTENTS)
    wants_disable = any(token in normalized for token in _MEDIA_PROXY_DISABLE_INTENTS)
    if wants_enable == wants_disable:
        return None
    numbers = _media_proxy_instance_numbers(normalized)
    if len(numbers) != 1:
        return None
    return {"instance_number": numbers[0], "enabled": wants_enable}


def is_media_proxy_control_message(message: str) -> bool:
    """识别启停意图本身；目标缺失/冲突时用于返回澄清而不是猜测。"""
    normalized = _normalize_intent_message(message)
    if not _has_media_proxy_scope(normalized) or _is_dangerous_action_discussion(normalized):
        return False
    if any(token in normalized for token in _MEDIA_PROXY_EDIT_REJECT_TOKENS):
        return False
    wants_enable = any(token in normalized for token in _MEDIA_PROXY_ENABLE_INTENTS)
    wants_disable = any(token in normalized for token in _MEDIA_PROXY_DISABLE_INTENTS)
    return wants_enable != wants_disable


def media_proxy_test_request(message: str) -> dict[str, int] | None:
    normalized = _normalize_intent_message(message)
    if not _has_media_proxy_scope(normalized):
        return None
    if any(token in normalized for token in _MEDIA_PROXY_EDIT_REJECT_TOKENS):
        return None
    if any(token in normalized for token in (*_MEDIA_PROXY_ENABLE_INTENTS, *_MEDIA_PROXY_DISABLE_INTENTS)):
        return None
    if not any(token in normalized for token in _MEDIA_PROXY_TEST_INTENTS):
        return None
    numbers = _media_proxy_instance_numbers(normalized)
    if len(numbers) != 1:
        return None
    return {"instance_number": numbers[0]}


def is_media_proxy_test_request_message(message: str) -> bool:
    """识别媒体反代测试意图；序号缺失/冲突时用于定向澄清。"""
    normalized = _normalize_intent_message(message)
    if not _has_media_proxy_scope(normalized):
        return False
    if any(token in normalized for token in _MEDIA_PROXY_EDIT_REJECT_TOKENS):
        return False
    if any(token in normalized for token in (*_MEDIA_PROXY_ENABLE_INTENTS, *_MEDIA_PROXY_DISABLE_INTENTS)):
        return False
    return any(token in normalized for token in _MEDIA_PROXY_TEST_INTENTS)


def is_media_proxy_status_summary_message(message: str) -> bool:
    normalized = _normalize_intent_message(message)
    if not _has_media_proxy_scope(normalized):
        return False
    if any(token in normalized for token in _MEDIA_PROXY_EDIT_REJECT_TOKENS):
        return False
    if any(token in normalized for token in (
        *_MEDIA_PROXY_ENABLE_INTENTS,
        *_MEDIA_PROXY_DISABLE_INTENTS,
        *_MEDIA_PROXY_TEST_INTENTS,
    )):
        return False
    return any(token in normalized for token in _MEDIA_PROXY_STATUS_INTENTS)


def is_discovery_search_message(message: str) -> bool:
    normalized = str(message or "").casefold()
    if any(token in normalized for token in _LOCAL_LIBRARY_TOKENS):
        return False
    return any(token in normalized for token in _DISCOVERY_SEARCH_TOKENS) and any(
        token in normalized for token in _DISCOVERY_SEARCH_VERBS
    )


_RECENT_DISCOVERY_REFERENCES = (
    "刚才搜索", "刚才的搜索", "搜索结果", "探索结果", "刚才推荐", "推荐结果",
)
_DISCOVERY_WATCHLIST_SCOPES = ("探索收藏", "影视收藏", "发现收藏")


def recent_discovery_candidate_request(message: str) -> dict[str, Any] | None:
    """解析最近探索候选的收藏或资源搜索续句。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if not normalized or not any(token in normalized for token in _RECENT_DISCOVERY_REFERENCES):
        return None
    position = _recent_resource_selection(normalized)
    if position is None:
        return None
    if "收藏" in normalized and any(token in normalized for token in (
        "加入", "添加", "收藏", "存下", "保存",
    )) and not any(token in normalized for token in ("移除", "删除", "取消收藏")):
        return {"action": "watchlist_add", "position": position}
    if any(token in normalized for token in (
        "找资源", "搜索资源", "搜资源", "找种子", "搜种子", "资源搜索",
    )):
        return {"action": "resource_search", "position": position}
    return None


def discovery_watchlist_summary_request(message: str) -> dict[str, Any] | None:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if not any(scope in normalized for scope in _DISCOVERY_WATCHLIST_SCOPES):
        return None
    if any(token in normalized for token in ("移除", "删除", "取消收藏", "加入", "添加")):
        return None
    matched = re.search(r"(?:编号|收藏)?\s*([0-9]{1,9})(?:\s*号)?", normalized)
    if matched:
        return {"watchlist_number": int(matched.group(1))}
    if any(token in normalized for token in ("列出", "全部", "列表", "有哪些", "查看", "看看", "查询")):
        return {}
    return None


def discovery_watchlist_remove_request(message: str) -> dict[str, Any] | None:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if not any(scope in normalized for scope in _DISCOVERY_WATCHLIST_SCOPES):
        return None
    if not any(token in normalized for token in ("移除", "删除", "取消收藏")):
        return None
    numbers = re.findall(r"(?:编号|收藏)?\s*([0-9]{1,9})(?:\s*号)?", normalized)
    if len(numbers) != 1:
        return None
    return {"watchlist_number": int(numbers[0])}


def is_discovery_watchlist_write_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold()
    return any(scope in normalized for scope in _DISCOVERY_WATCHLIST_SCOPES) and any(
        token in normalized for token in ("移除", "删除", "取消收藏", "加入", "添加")
    )


def _parse_human_number(value: object) -> int | None:
    """解析阿拉伯数字与常用中文数词，供候选序号和集号共用。"""
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    digits = {
        "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
        "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    }
    units = {"十": 10, "百": 100, "千": 1000}
    if not all(char in digits or char in units for char in text):
        return None
    if not any(char in units for char in text):
        try:
            return int("".join(str(digits[char]) for char in text))
        except (KeyError, ValueError):
            return None
    total = 0
    current = 0
    for char in text:
        if char in digits:
            current = digits[char]
            continue
        unit = units[char]
        total += (current or 1) * unit
        current = 0
    return total + current


def _recent_resource_selection(message: str) -> int | None:
    normalized = unicodedata.normalize("NFKC", str(message or ""))
    matched = re.search(
        r"(?:第\s*([0-9]{1,3}|[零〇一二两三四五六七八九十百千]{1,7})\s*(?:个|项|条|部)"
        r"|([0-9]{1,3}|[零〇一二两三四五六七八九十百千]{1,7})\s*号)",
        normalized,
    )
    if not matched:
        return None
    selection = _parse_human_number(matched.group(1) or matched.group(2))
    return selection if selection is not None and 1 <= selection <= 1000 else None


def _recent_resource_episode(message: str) -> int | None:
    """解析“第 34 集 / 第三十四集”这类内容集数，避免误当候选序号。"""
    normalized = unicodedata.normalize("NFKC", str(message or ""))
    matched = re.search(
        r"(?:第\s*)?([0-9]{1,3}|[零〇一二两三四五六七八九十百千]{1,7})\s*(?:集|话)(?![数目])",
        normalized,
    )
    if not matched:
        return None
    episode = _parse_human_number(matched.group(1))
    return episode if episode is not None and 1 <= episode <= 1000 else None


def _candidate_episode(candidate: Any) -> int | None:
    if not isinstance(candidate, dict):
        return None
    value = candidate.get("episode")
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 1000:
        return value
    title = unicodedata.normalize("NFKC", str(candidate.get("title") or ""))
    patterns = (
        r"(?i)S\d{1,3}\s*E(?:P)?\s*0*([0-9]{1,3})(?!\d)",
        r"(?:第\s*)?0*([0-9]{1,3})\s*(?:集|话)(?![数目])",
        r"[\[【(（]\s*0*([0-9]{1,3})\s*[\]】)）]",
    )
    for pattern in patterns:
        matched = re.search(pattern, title)
        if matched:
            episode = int(matched.group(1))
            if 1 <= episode <= 1000:
                return episode
    chinese = re.search(
        r"(?:第\s*)?([零〇一二两三四五六七八九十百千]{1,7})\s*(?:集|话)(?![数目])",
        title,
    )
    if chinese:
        episode = _parse_human_number(chinese.group(1))
        if episode is not None and 1 <= episode <= 1000:
            return episode
    return None


def _recent_resource_target(message: str) -> tuple[str | None, bool]:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    has_qb = bool(re.search(r"(?<![a-z])q(?:b|bittorrent)(?![a-z])", normalized))
    has_guangya = "光鸭" in normalized
    explicit_both = any(token in normalized for token in (
        "两个后端", "两个目标", "两边", "两者", "都推送", "都下载", "全部推送", "同时推送",
    ))
    has_target = has_qb or has_guangya or explicit_both
    if not has_target:
        return None, False
    ambiguous_dual = has_qb and has_guangya and any(
        token in normalized for token in ("或", "还是", "任一", "任选")
    )
    linked_dual = has_qb and has_guangya and bool(re.search(
        r"(?:q(?:b|bittorrent).{0,12}(?:和|与|及|以及|跟).{0,12}光鸭|"
        r"光鸭.{0,12}(?:和|与|及|以及|跟).{0,12}q(?:b|bittorrent))",
        normalized,
    ))
    if ambiguous_dual:
        return None, True
    if explicit_both or linked_dual:
        return "both", True
    if has_qb and not has_guangya:
        return "qb", True
    if has_guangya and not has_qb:
        return "guangya", True
    return None, True


def _recent_resource_target_followup_request(
    message: str,
    conversation_context: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """承接上一轮已经选定候选、只缺下载目标的安全追问。"""
    previous = _last_assistant_context(conversation_context)
    if (
        str(previous.get("tool_name") or "").strip() != "indexer.submit_resource"
        or str(previous.get("status") or "").strip().casefold() != "selection_required"
    ):
        return None
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if not normalized or any(token in normalized for token in _RECENT_RESOURCE_REJECT_TOKENS):
        return None
    target, has_target = _recent_resource_target(normalized)
    if not has_target:
        return None
    position = _recent_resource_selection(previous.get("text"))
    if position is None:
        return None
    return {"position": position, "target": target}


def recent_resource_submit_request(
    message: str, *, allow_implicit: bool = False
) -> dict[str, Any] | None:
    """解析最近资源候选的确认准备请求；始终只进入预检，不直接下载。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if not normalized or any(token in normalized for token in _RECENT_RESOURCE_REJECT_TOKENS):
        return None

    selection = _recent_resource_selection(normalized)
    episode = _recent_resource_episode(normalized)
    has_reference = any(token in normalized for token in _RECENT_RESOURCE_REFERENCES)
    has_action = any(token in normalized for token in _RECENT_RESOURCE_ACTIONS) or any(
        token in normalized for token in ("下到", "下去", "就要")
    )
    if has_reference:
        if not has_action:
            return None
    elif not allow_implicit or (selection is None and episode is None):
        return None

    target, _ = _recent_resource_target(normalized)
    request = {"position": selection, "target": target}
    if episode is not None:
        request["episode"] = episode
    return request


def is_recent_resource_submit_message(message: str) -> bool:
    return recent_resource_submit_request(message) is not None


_RECENT_RESOURCE_PRE_MODEL_REJECT_PATTERN = re.compile(
    r"(?:搜索|检索|查找|寻找|找\s*(?:资源|种子|磁力|下载源)|巡检|缺集|"
    r"下载队列|下载任务|任务队列|rss|订阅|媒体库)",
    re.IGNORECASE,
)


def _recent_resource_pre_model_submit_request(
    message: str,
    conversation_context: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """只抢占明确的最近候选提交续句，避免集数/序号误伤其他业务意图。"""
    explicit = recent_resource_submit_request(message)
    if explicit is not None:
        return explicit
    contextual_target = _recent_resource_target_followup_request(
        message, conversation_context
    )
    if contextual_target is not None:
        return contextual_target

    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if not normalized or _RECENT_RESOURCE_PRE_MODEL_REJECT_PATTERN.search(normalized):
        return None
    implicit = recent_resource_submit_request(message, allow_implicit=True)
    if implicit is None:
        return None
    has_transfer_intent = any(
        token in normalized
        for token in (*_RECENT_RESOURCE_ACTIONS, "下到", "下去", "就要")
    )
    if not has_transfer_intent and implicit.get("target") is None:
        return None
    return implicit


def recent_download_status_request(message: str) -> dict[str, int | None] | None:
    """解析最近一次已确认资源提交的只读状态续接。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if not normalized or any(token in normalized for token in _RECENT_DOWNLOAD_REJECT_TOKENS):
        return None
    selection = _recent_patrol_selection(normalized)
    has_reference = any(token in normalized for token in _RECENT_DOWNLOAD_REFERENCES)
    if selection is None and not has_reference:
        return None
    if not any(token in normalized for token in _RECENT_DOWNLOAD_SCOPES):
        return None
    if not any(token in normalized for token in _RECENT_DOWNLOAD_STATUS_MARKERS):
        return None
    return {"position": selection}


def is_recent_download_status_message(message: str) -> bool:
    return recent_download_status_request(message) is not None


def recent_download_library_verification_request(message: str) -> dict[str, int | None] | None:
    """解析最近缺集下载的只读媒体库核验请求。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if not normalized or any(token in normalized for token in _RECENT_DOWNLOAD_LIBRARY_REJECT_TOKENS):
        return None
    selection = _recent_patrol_selection(normalized)
    has_reference = any(token in normalized for token in _RECENT_DOWNLOAD_REFERENCES)
    if selection is None and not has_reference:
        return None
    if not any(token in normalized for token in _RECENT_DOWNLOAD_SCOPES):
        return None
    if not any(token in normalized for token in _RECENT_DOWNLOAD_LIBRARY_MARKERS):
        return None
    if not any(token in normalized for token in _RECENT_DOWNLOAD_LIBRARY_INTENTS):
        return None
    return {"position": selection}


def is_recent_download_library_verification_message(message: str) -> bool:
    return recent_download_library_verification_request(message) is not None


def recent_download_explanation_request(message: str) -> dict[str, int | None] | None:
    """解析最近一次已确认资源提交的只读异常解释续接。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if not normalized or any(token in normalized for token in _RECENT_DOWNLOAD_EXPLANATION_REJECT_TOKENS):
        return None
    selection = _recent_patrol_selection(normalized)
    has_reference = any(token in normalized for token in _RECENT_DOWNLOAD_REFERENCES)
    if selection is None and not has_reference:
        return None
    if not any(token in normalized for token in _RECENT_DOWNLOAD_SCOPES):
        return None
    if not any(token in normalized for token in _RECENT_DOWNLOAD_EXPLANATION_MARKERS):
        return None
    return {"position": selection}


def is_recent_download_explanation_message(message: str) -> bool:
    return recent_download_explanation_request(message) is not None


_WEB_SEARCH_MARKERS = (
    "网页搜索", "搜索网页", "查网页", "网络搜索", "联网搜索",
    "上网查", "上网搜索", "web search", "用 tavily", "tavily 搜",
)


def is_web_search_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    return any(marker in normalized for marker in _WEB_SEARCH_MARKERS)


def _extract_web_search_query(message: str) -> str:
    query = unicodedata.normalize("NFKC", str(message or "")).strip()
    for marker in sorted(_WEB_SEARCH_MARKERS, key=len, reverse=True):
        query = re.sub(re.escape(marker), " ", query, flags=re.IGNORECASE)
    query = re.sub(r"^(?:请|帮我|给我|麻烦)?\s*(?:一下)?\s*", "", query)
    query = re.sub(r"\s*(?:一下)?\s*$", "", query)
    return query.strip(" ，。！？?、:：")[:200]


def is_discovery_recommend_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if any(token in normalized for token in _DISCOVERY_RECOMMEND_REJECT_TOKENS):
        return False
    if is_discovery_search_message(normalized):
        return False
    return any(anchor in normalized for anchor in _DISCOVERY_RECOMMEND_ANCHORS)


def is_contextual_discovery_recommend_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    return (
        not is_discovery_search_message(normalized)
        and any(anchor in normalized for anchor in _DISCOVERY_RECOMMEND_ANCHORS)
        and any(token in normalized for token in _DISCOVERY_CONTEXTUAL_RECOMMEND_TOKENS)
    )


def _today_iso_weekday() -> int:
    return date.today().isoweekday()


def is_bangumi_calendar_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if any(token in normalized for token in _LOCAL_LIBRARY_TOKENS):
        return False
    if is_indexer_resource_search_message(normalized):
        return False
    quoted = bool(re.search(r"[《「『\"'][^》」』\"']+[》」』\"']", normalized))
    explicit_lookup = bool(re.search(
        r"(?:搜索|查询|查找|查一下|找一下|找找|搜)(?:\s|[:：]|《|「|『|\")",
        normalized,
    ))
    if explicit_lookup:
        return False
    if quoted and any(verb in normalized for verb in _DISCOVERY_SEARCH_VERBS):
        return False
    has_content = any(token in normalized for token in _BANGUMI_CALENDAR_CONTENT_TOKENS)
    has_weekday = any(
        alias in normalized for aliases in _WEEKDAY_ALIASES.values() for alias in aliases
    )
    has_schedule = (
        any(token in normalized for token in _BANGUMI_CALENDAR_MARKERS)
        or any(token in normalized for token in _BANGUMI_TODAY_TOKENS)
        or any(token in normalized for token in _BANGUMI_WEEK_TOKENS)
        or (
            has_weekday
            and any(intent in normalized for intent in _BANGUMI_CALENDAR_LIST_INTENTS)
        )
    )
    return has_content and has_schedule


def _bangumi_calendar_arguments(message: str) -> dict[str, Any]:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold()
    weekday: int | None = None
    for value, aliases in _WEEKDAY_ALIASES.items():
        if any(alias in normalized for alias in aliases):
            weekday = value
            break
    if weekday is None and any(token in normalized for token in _BANGUMI_TODAY_TOKENS):
        weekday = _today_iso_weekday()
    arguments: dict[str, Any] = {"page": 1, "limit": 10}
    if weekday is not None:
        arguments["weekday"] = weekday
    return arguments


def _discovery_recommend_arguments(message: str) -> dict[str, Any]:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold()
    provider = "douban" if "豆瓣" in normalized else "tmdb"
    if any(token in normalized for token in _DISCOVERY_TV_TOKENS):
        media_type = "tv"
    elif any(token in normalized for token in _DISCOVERY_MOVIE_TOKENS):
        media_type = "movie"
    else:
        media_type = "movie"
    return {"provider": provider, "media_type": media_type, "page": 1, "limit": 10}


def _extract_discovery_search_query(message: str) -> str:
    query = _extract_search_query(message)
    if not query:
        matched = re.search(
            r"(?:网上|外部|影视探索|探索页|tmdb|豆瓣|bangumi|bgm)\s*"
            r"(?:找一下|找找|搜索|查询|查一下|查找|找|搜|查)\s*[:：]?\s*(.+)",
            message,
            flags=re.IGNORECASE,
        )
        query = matched.group(1).strip() if matched else ""
    if not query:
        return ""
    query = re.sub(
        r"^(?:网上|外部|影视探索|探索页|tmdb|豆瓣|bangumi|bgm)\s*[:：]?\s*",
        "",
        query,
        flags=re.IGNORECASE,
    )
    return query.strip(" ，。！？?、:：")[:120]


def is_workspace_search_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold()
    if any(token in normalized for token in _WORKSPACE_TRACE_ANCHORS):
        return True
    has_workspace_intent = any(anchor in normalized for anchor in _WORKSPACE_SEARCH_ANCHORS) and any(
        verb in normalized for verb in _WORKSPACE_SEARCH_VERBS
    )
    if has_workspace_intent:
        return True
    if any(token in normalized for token in _RESOURCE_SEARCH_TOKENS):
        return False
    if any(token in normalized for token in _DISCOVERY_SEARCH_TOKENS):
        return False
    return False


def _workspace_search_sections(message: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold()
    if any(anchor in normalized for anchor in _WORKSPACE_GLOBAL_ANCHORS):
        return []
    scope_text = re.sub(r"[《「『\"'].*?[》」』\"']", "", normalized)
    boundary = re.search(
        r"(?:找一下|找找|搜索|查找|查询|查一下|找|搜|查|处理到哪|走到哪一步|到哪一步)",
        scope_text,
    )
    if not boundary:
        return []
    scope_text = scope_text[:boundary.start()]
    sections: list[str] = []
    mapping = (
        ("library", ("媒体库", "jellyfin", "emby", "本地库", "我的库")),
        ("rss", ("rss", "订阅")),
        ("downloads", ("下载记录", "下载历史", "下载任务")),
        ("organize", ("整理日志", "整理记录", "云盘整理", "光鸭整理")),
        ("local_media", ("本地媒体", "本地整理", "本地入库")),
    )
    for section, tokens in mapping:
        if any(token in scope_text for token in tokens):
            sections.append(section)
    return sections


def _extract_workspace_search_query(message: str) -> str:
    quoted = re.search(r"[《「『\"']([^》」』\"']{1,120})[》」』\"']", message)
    if quoted:
        return quoted.group(1).strip()
    trace = re.search(r"(.+?)(?:现在)?(?:处理到哪|走到哪一步|到哪一步)", message, flags=re.IGNORECASE)
    if trace:
        query = trace.group(1)
    else:
        query = _extract_search_query(message)
    if not query:
        return ""
    query = re.sub(
        r"^(?:请|帮我|在)?\s*(?:全局|工作区|项目内|控制台)?\s*"
        r"(?:下载记录|下载历史|整理日志|整理记录|订阅记录|rss\s*订阅)?\s*"
        r"(?:里|中|内)?\s*(?:找一下|找找|搜索|查找|查询|查一下|找|搜|查)?\s*[:：]?\s*",
        "",
        query,
        flags=re.IGNORECASE,
    )
    return query.strip(" ，。！？?、:：")[:120]


def is_indexer_resource_search_message(message: str) -> bool:
    normalized = str(message or "").casefold()
    return (
        any(token in normalized for token in _RESOURCE_SEARCH_TOKENS)
        and any(token in normalized for token in _RESOURCE_SEARCH_VERBS)
    )


def _extract_resource_search_query(message: str) -> str:
    query = _extract_search_query(message)
    if not query:
        return ""
    query = re.sub(
        r"^(?:资源|种子|磁力(?:链接)?|下载源|资源站)\s*[:：]?\s*",
        "",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        r"\s*(?:的)?(?:资源|种子|磁力(?:链接)?|下载源|资源站)\s*$",
        "",
        query,
        flags=re.IGNORECASE,
    )
    return query.strip(" ，。！？?、:：")[:120]


def indexer_site_change_followup_request(
    message: str,
    conversation_context: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """解析“开启站点后继续搜索”复合命令，并从近期对话承接片名。"""
    normalized = _normalize_indexer_command(message)
    boundary = re.search(
        r"(?:之后|然后|并且|同时|后|[,，;；])?\s*(?:继续|再)?\s*"
        r"(?:搜索|检索|搜|找(?:一下)?资源)",
        normalized,
    )
    if boundary is None:
        return None
    change = indexer_site_change_request(normalized[:boundary.start()].strip())
    if change is None or change.get("operation") == "clarify_scope_conflict":
        return None
    search_part = normalized[boundary.start():]
    title = _extract_resource_search_query(search_part)
    normalized_title = re.sub(r"[\s，。！？?、:：]+", "", str(title or "").casefold())
    if normalized_title in {
        "上一片名", "上个片名", "上一次片名", "刚才片名", "刚才那部",
        "上一部", "上一个", "刚才的", "刚才那个",
    }:
        title = ""
    if not title:
        for item in reversed(conversation_context or []):
            if not isinstance(item, dict) or str(item.get("role") or "").casefold() != "user":
                continue
            prior_message = str(
                item.get("text") or item.get("message") or item.get("content") or ""
            ).strip()
            if is_indexer_resource_search_message(prior_message):
                title = _extract_resource_search_query(prior_message)
                if title:
                    break
    if not title:
        media_context = _latest_media_context(conversation_context)
        title = str(media_context.get("title") or "").strip()
    if not title:
        return None
    return {"change": change, "title": title}


_EPISODE_AUDIT_TOKENS = (
    "缺集", "漏集", "完整性", "核对剧集", "缺少的剧集", "少了哪些集",
    "有没有缺少", "是否缺少",
)
_LOCAL_EPISODE_COUNT_SCOPES = (
    "媒体库", "本地库", "我的库", "库里", "jellyfin", "emby",
)
_LOCAL_EPISODE_COUNT_REJECT_TOKENS = (
    "缺集", "漏集", "缺少", "少了", "完整性", "有没有更新", "有无更新",
    "是否更新", "是否有更新", "更新了吗", "新一集", "新一季", "资源", "种子", "磁力",
)
_LIBRARY_UPDATE_TOKENS = (
    "有没有更新", "有无更新", "是否更新", "是否有更新", "更新了吗", "有更新吗", "新一集", "新一季",
)
_LIBRARY_UPDATE_REJECT_TOKENS = (
    "配置", "设置", "项目", "环境", "rss", "订阅", "下载任务", "下载队列",
    "刷新媒体库", "扫描媒体库", "重新扫描", "同步媒体库",
)
_LIBRARY_UPDATE_NON_MEDIA_TOKENS = (
    "读书", "阅读", "工作进度", "任务进度", "软件", "应用", "程序", "代码",
    "文档", "系统版本", "版本更新", "固件", "驱动", "插件",
)
_MISSING_EPISODE_RESOURCE_TOKENS = (
    "缺集", "漏集", "少集", "补集", "缺少", "缺失", "缺的", "本地没有", "库里没有",
)


_MISSING_SEASON_RESOURCE_REJECT_TOKENS = (
    "要不要", "是否", "能否", "可否", "有没有", "有无", "不搜", "别搜", "不要搜", "不用搜",
)
_MISSING_SEASON_RESOURCE_COMMAND_TOKENS = ("找", "搜", "搜索", "查找")


_HUMAN_NUMBER_TOKEN_RE = r"(?:[0-9]{1,4}|[零〇一二两三四五六七八九十百千]{1,7})"


def _episode_coordinates(message: str) -> tuple[int, int] | None:
    human = re.search(
        rf"第\s*({_HUMAN_NUMBER_TOKEN_RE})\s*季\s*第\s*"
        rf"({_HUMAN_NUMBER_TOKEN_RE})\s*集",
        message,
        re.IGNORECASE,
    )
    if human:
        season = _parse_human_number(human.group(1))
        episode = _parse_human_number(human.group(2))
        if season is not None and episode is not None and 1 <= season <= 100 and 1 <= episode <= 1000:
            return season, episode

    for pattern in (
        r"(?<![A-Za-z0-9])S([0-9]{1,3})\s*E([0-9]{1,4})(?![A-Za-z0-9])",
        r"(?<![A-Za-z0-9])([0-9]{1,3})x([0-9]{1,4})(?![A-Za-z0-9])",
    ):
        matched = re.search(pattern, message, re.IGNORECASE)
        if matched:
            season, episode = int(matched.group(1)), int(matched.group(2))
            if 1 <= season <= 100 and 1 <= episode <= 1000:
                return season, episode
    return None


def is_missing_episode_resource_search_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold()
    return (
        bool(_episode_coordinates(normalized))
        and is_indexer_resource_search_message(normalized)
    )


def _season_coordinate(message: str) -> int | None:
    human = re.search(
        rf"第\s*({_HUMAN_NUMBER_TOKEN_RE})\s*季",
        message,
        re.IGNORECASE,
    )
    if human:
        season = _parse_human_number(human.group(1))
        if season is not None and 1 <= season <= 100:
            return season
    matched = re.search(
        r"(?<![A-Za-z0-9])S([0-9]{1,3})(?!\s*E[0-9])",
        message,
        re.IGNORECASE,
    )
    if matched:
        season = int(matched.group(1))
        if 1 <= season <= 100:
            return season
    return None


def is_missing_season_resource_search_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold()
    if any(token in normalized for token in _MISSING_SEASON_RESOURCE_REJECT_TOKENS):
        return False
    if normalized.rstrip().endswith(("吗", "？", "?")):
        return False
    return (
        _episode_coordinates(normalized) is None
        and _season_coordinate(normalized) is not None
        and any(token in normalized for token in _MISSING_EPISODE_RESOURCE_TOKENS)
        and any(token in normalized for token in _MISSING_SEASON_RESOURCE_COMMAND_TOKENS)
        and is_indexer_resource_search_message(normalized)
    )


def _extract_missing_season_resource_args(message: str) -> dict[str, Any]:
    season = _season_coordinate(message)
    if season is None or _episode_coordinates(message) is not None:
        return {}
    arguments: dict[str, Any] = {"season": season}
    library_name = _extract_named_library_scope(message)
    if library_name:
        arguments["library_name"] = library_name

    quoted = re.search(r"[《「『\"']([^》」』\"']{1,120})[》」』\"']", message)
    if quoted:
        arguments["query"] = quoted.group(1).strip()
    else:
        cleaned = _strip_named_library_scope(message)
        cleaned = re.sub(r"\btmdb\s*(?:id)?\s*[:：#]?\s*[0-9]{1,10}\b", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(rf"第\s*{_HUMAN_NUMBER_TOKEN_RE}\s*季", "", cleaned)
        cleaned = re.sub(r"(?<![A-Za-z0-9])S[0-9]{1,3}(?!\s*E[0-9])", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(
            r"(?:的)?(?:所有|全部|整季|这季|本季|该季|这一季|批量|多集)?"
            r"(?:缺集|漏集|少集|补集|缺少|缺失|缺的)(?:的)?"
            r"(?:找一下|找找|搜索|查找|找|搜)?\s*"
            r"(?:资源|种子|磁力(?:链接)?|下载源|资源站)",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"^(?:请帮我|麻烦帮我|帮我|帮|给我|请|给)?\s*(?:找一下|找找|搜索|查找|找|搜)?\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"\s*(?:批量\s*)?(?:找一下|找找|搜索|查找|找|搜)\s*$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        query = cleaned.strip(" ，。！？?、:：")
        if query:
            arguments["query"] = query[:120]

    tmdb = re.search(r"\btmdb\s*(?:id)?\s*[:：#]?\s*([0-9]{1,10})\b", message, re.IGNORECASE)
    if tmdb:
        arguments["tmdb_id"] = tmdb.group(1)
    return arguments


def _extract_missing_episode_resource_args(message: str) -> dict[str, Any]:
    coordinates = _episode_coordinates(message)
    if coordinates is None:
        return {}
    season, episode = coordinates
    arguments: dict[str, Any] = {"season": season, "episode": episode}
    library_name = _extract_named_library_scope(message)
    if library_name:
        arguments["library_name"] = library_name

    quoted = re.search(r"[《「『\"']([^》」』\"']{1,120})[》」』\"']", message)
    if quoted:
        arguments["query"] = quoted.group(1).strip()
    else:
        cleaned = _strip_named_library_scope(message)
        cleaned = re.sub(r"\btmdb\s*(?:id)?\s*[:：#]?\s*[0-9]{1,10}\b", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(
            rf"第\s*{_HUMAN_NUMBER_TOKEN_RE}\s*季\s*第\s*"
            rf"{_HUMAN_NUMBER_TOKEN_RE}\s*集",
            "",
            cleaned,
        )
        cleaned = re.sub(
            r"(?<![A-Za-z0-9])S[0-9]{1,3}\s*E[0-9]{1,4}(?![A-Za-z0-9])"
            r"|(?<![A-Za-z0-9])[0-9]{1,3}x[0-9]{1,4}(?![A-Za-z0-9])",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"(?:的)?(?:缺集|漏集|少集|补集|缺少|缺失)?(?:资源|种子|磁力(?:链接)?|下载源|资源站)",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"^(?:请帮我|麻烦帮我|帮我|给我|请|给)?\s*(?:找一下|找找|搜索|查找|找|搜)?\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"\s*(?:找一下|找找|搜索|查找|找|搜)\s*$", "", cleaned, flags=re.IGNORECASE)
        query = cleaned.strip(" ，。！？?、:：")
        if query:
            arguments["query"] = query[:120]

    tmdb = re.search(r"\btmdb\s*(?:id)?\s*[:：#]?\s*([0-9]{1,10})\b", message, re.IGNORECASE)
    if tmdb:
        arguments["tmdb_id"] = tmdb.group(1)
    return arguments


_LIBRARY_EPISODE_PATROL_SCOPES = (
    "全库", "整库", "整个媒体库", "全部媒体库", "所有剧集", "全部剧集", "媒体库所有剧",
)
_LIBRARY_EPISODE_PATROL_INTENTS = (
    "巡检", "体检", "完整性", "缺集", "漏集", "有没有缺", "是否缺",
)

_RECENT_PATROL_REFERENCES = (
    "刚才巡检", "上次巡检", "最近巡检", "巡检结果", "巡检发现", "刚巡检",
)
_RECENT_PATROL_RESOURCE_ACTIONS = (
    "找资源", "搜索资源", "搜资源", "找种子", "搜索种子", "搜种子", "找磁力", "搜索磁力",
)
_RECENT_PATROL_REJECT_TOKENS = (
    "如何", "怎么", "配置", "设置", "说明", "支持什么", "能做什么", "不要", "不用", "别找",
)
_CHINESE_SELECTION_NUMBERS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _is_recent_library_patrol_resource_reference(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold()
    has_resource_action = (
        any(token in normalized for token in _RECENT_PATROL_RESOURCE_ACTIONS)
        or (
            any(token in normalized for token in _RESOURCE_SEARCH_VERBS)
            and any(token in normalized for token in _RESOURCE_SEARCH_TOKENS)
        )
    )
    return any(token in normalized for token in _RECENT_PATROL_REFERENCES) and has_resource_action


def is_recent_library_patrol_resource_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold()
    return (
        _is_recent_library_patrol_resource_reference(normalized)
        and not any(token in normalized for token in _RECENT_PATROL_REJECT_TOKENS)
    )


_LIBRARY_PATROL_POLICY_SCOPES = (
    "全库缺集巡检", "全库巡检", "媒体库巡检", "自动缺集巡检", "自动巡检", "定时巡检",
)
_LIBRARY_PATROL_POLICY_ACTION_REJECT_TOKENS = (
    "不想", "不用", "无需", "不要", "没有必要", "取消", "禁止", "勿", "未", "不", "别",
    "如果", "假如", "要是", "能否", "可以吗", "是否可以", "会怎样",
    "有什么风险", "为什么", "如何", "怎么", "吗", "呢", "？", "?",
)
_LIBRARY_PATROL_POLICY_UNRELATED_SCOPES = (
    "媒体探索", "豆瓣探索", "多站资源搜索", "资源站点", "联网搜索", "网页搜索",
    "下载任务", "rss", "strm", "云盘整理", "光鸭整理",
)
_LIBRARY_PATROL_ENABLE_VERBS = ("开启", "启用", "打开")
_LIBRARY_PATROL_DISABLE_VERBS = ("关闭", "停用", "禁用")


def _single_numeric_policy_value(
    message: str,
    pattern: str,
    *,
    label: str,
) -> int | None:
    values = {int(value) for value in re.findall(pattern, message)}
    if len(values) > 1:
        raise AgentToolError(f"{label}出现多个冲突值")
    return next(iter(values), None)


def _policy_toggle_value(message: str, target_pattern: str) -> bool | None:
    enabled = bool(re.search(
        rf"(?:{'|'.join(_LIBRARY_PATROL_ENABLE_VERBS)})\s*{target_pattern}|"
        rf"{target_pattern}\s*(?:{'|'.join(_LIBRARY_PATROL_ENABLE_VERBS)})",
        message,
    ))
    disabled = bool(re.search(
        rf"(?:{'|'.join(_LIBRARY_PATROL_DISABLE_VERBS)})\s*{target_pattern}|"
        rf"{target_pattern}\s*(?:{'|'.join(_LIBRARY_PATROL_DISABLE_VERBS)})",
        message,
    ))
    if enabled and disabled:
        raise AgentToolError("巡检策略包含互相冲突的开关动作")
    if enabled:
        return True
    if disabled:
        return False
    return None


def library_patrol_policy_request(message: str) -> dict[str, Any] | None:
    """只识别明确、肯定且边界固定的全库巡检策略修改。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if not normalized or not any(scope in normalized for scope in _LIBRARY_PATROL_POLICY_SCOPES):
        return None
    if any(token in normalized for token in _LIBRARY_PATROL_POLICY_ACTION_REJECT_TOKENS):
        return None
    action_words = _LIBRARY_PATROL_ENABLE_VERBS + _LIBRARY_PATROL_DISABLE_VERBS
    if any(scope in normalized for scope in _LIBRARY_PATROL_POLICY_UNRELATED_SCOPES) and any(
        verb in normalized for verb in action_words
    ):
        return None

    arguments: dict[str, Any] = {}
    numeric_change_requested = any(token in normalized for token in (
        "设置", "设为", "改为", "改成", "调整", "修改", "变更",
    ))
    if numeric_change_requested:
        interval_hours = _single_numeric_policy_value(
            normalized,
            r"(?:每|间隔(?:为|设为|设置为|改为)?|周期(?:为|设为|设置为|改为)?)\s*([0-9]{1,3})\s*小时",
            label="巡检间隔",
        )
        if interval_hours is not None:
            arguments["interval_hours"] = interval_hours
        max_series = _single_numeric_policy_value(
            normalized,
            r"(?:最多(?:检查|巡检|扫描|核对)?|单轮(?:最多)?(?:检查|巡检|扫描|核对)?(?:上限)?(?:为|设为|设置为|改为)?|检查上限(?:为|设为|设置为|改为)?)\s*([0-9]{1,3})\s*(?:部|个)(?:剧|剧集)?",
            label="单轮检查上限",
        )
        if max_series is not None:
            arguments["max_series"] = max_series

    notification_target = (
        r"(?:(?:全库|媒体库|自动缺集|自动|定时)?巡检(?:结果)?)?通知"
    )
    notify_enabled = _policy_toggle_value(normalized, notification_target)
    if notify_enabled is not None:
        arguments["notify_enabled"] = notify_enabled

    patrol_target = r"(?:后台)?(?:全库缺集巡检|全库巡检|媒体库巡检|自动缺集巡检|自动巡检|定时巡检)"
    without_notification_phrases = re.sub(
        rf"(?:{'|'.join(_LIBRARY_PATROL_ENABLE_VERBS + _LIBRARY_PATROL_DISABLE_VERBS)})"
        rf"\s*{notification_target}|{notification_target}\s*"
        rf"(?:{'|'.join(_LIBRARY_PATROL_ENABLE_VERBS + _LIBRARY_PATROL_DISABLE_VERBS)})",
        "",
        normalized,
    )
    enabled = _policy_toggle_value(without_notification_phrases, patrol_target)
    if enabled is not None:
        arguments["enabled"] = enabled

    return arguments or None


def is_library_patrol_policy_summary_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if not normalized or library_patrol_policy_request(normalized) is not None:
        return False
    if not any(scope in normalized for scope in _LIBRARY_PATROL_POLICY_SCOPES):
        return False
    if any(token in normalized for token in ("上次", "最近", "结果", "发现", "缺了几集")):
        return False
    return any(token in normalized for token in (
        "策略", "配置", "周期", "间隔", "频率", "通知", "单轮", "上限",
        "当前", "现在", "查看", "看看", "怎么配置", "如何配置",
    ))


_STRM_SCHEDULE_REJECT_TOKENS = (
    "不要", "不想", "不用", "无需", "取消", "能否", "可以吗", "会不会", "如果",
    "然后", "同时把", "顺便", "以及关闭", "以及开启",
)
_STRM_SCHEDULE_ENABLE_VERBS = ("开启", "启用", "打开")
_STRM_SCHEDULE_DISABLE_VERBS = ("关闭", "停用", "禁用")


def _strm_policy_toggle_value(message: str, target_pattern: str) -> bool | None:
    enabled = bool(re.search(
        rf"(?:{'|'.join(_STRM_SCHEDULE_ENABLE_VERBS)})\s*{target_pattern}|"
        rf"{target_pattern}\s*(?:{'|'.join(_STRM_SCHEDULE_ENABLE_VERBS)})",
        message,
    ))
    disabled = bool(re.search(
        rf"(?:{'|'.join(_STRM_SCHEDULE_DISABLE_VERBS)})\s*{target_pattern}|"
        rf"{target_pattern}\s*(?:{'|'.join(_STRM_SCHEDULE_DISABLE_VERBS)})",
        message,
    ))
    if enabled and disabled:
        raise AgentToolError("STRM 调度策略包含互相冲突的开关动作")
    if enabled:
        return True
    if disabled:
        return False
    return None


def strm_schedule_policy_request(message: str) -> dict[str, Any] | None:
    """只识别明确、肯定且边界固定的 STRM 调度策略修改。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if not normalized or "strm" not in normalized:
        return None
    if any(token in normalized for token in _STRM_SCHEDULE_REJECT_TOKENS):
        return None

    arguments: dict[str, Any] = {}
    notification_target = r"strm\s*(?:任务|同步|定时同步)?\s*通知"
    notify_enabled = _strm_policy_toggle_value(normalized, notification_target)
    if notify_enabled is not None:
        arguments["notify_enabled"] = notify_enabled

    schedule_target = r"strm\s*(?:定时同步|定时任务|调度|计划任务)"
    without_notification = re.sub(
        rf"(?:{'|'.join(_STRM_SCHEDULE_ENABLE_VERBS + _STRM_SCHEDULE_DISABLE_VERBS)})"
        rf"\s*{notification_target}|{notification_target}\s*"
        rf"(?:{'|'.join(_STRM_SCHEDULE_ENABLE_VERBS + _STRM_SCHEDULE_DISABLE_VERBS)})",
        "",
        normalized,
    )
    enabled = _strm_policy_toggle_value(without_notification, schedule_target)
    if enabled is not None:
        arguments["enabled"] = enabled

    explicit_cron = re.search(
        r"(?:cron|调度表达式|计划表达式)(?:\s*表达式)?\s*(?:设为|设置为|改为|改成|[:：])\s*"
        r"([*0-9,/?L#W-]+(?:\s+[*0-9,/?L#W-]+){4})",
        normalized,
    )
    if explicit_cron:
        arguments["cron"] = " ".join(explicit_cron.group(1).strip().split())
    else:
        daily_hour = re.search(
            r"(?:改为|改成|设置为|设为)\s*(?:每天|每日)\s*([0-9]{1,2})\s*(?:点|时)",
            normalized,
        )
        if daily_hour:
            hour = int(daily_hour.group(1))
            if 0 <= hour <= 23:
                arguments["cron"] = f"0 {hour} * * *"
            else:
                raise AgentToolError("STRM 每日运行小时必须为 0 到 23")

    return arguments or None


def is_strm_schedule_policy_summary_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if not normalized or "strm" not in normalized:
        return False
    if strm_schedule_policy_request(normalized) is not None:
        return False
    if any(token in normalized for token in ("运行状态", "同步状态", "进度", "失败", "重试")):
        return False
    return any(token in normalized for token in (
        "调度策略", "定时策略", "计划表达式", "调度表达式", "cron", "定时配置",
        "查看定时", "看看定时", "当前定时", "通知开关",
    ))


_GUANGYA_SCHEDULE_REJECT_TOKENS = (
    "不要", "不想", "不用", "无需", "取消", "能否", "可以吗", "会不会", "如果",
    "然后", "同时把", "顺便", "以及关闭", "以及开启",
)
_GUANGYA_SCHEDULE_ENABLE_VERBS = ("开启", "启用", "打开")
_GUANGYA_SCHEDULE_DISABLE_VERBS = ("关闭", "停用", "禁用")


def _guangya_schedule_toggle_value(message: str, target_pattern: str) -> bool | None:
    enabled = bool(re.search(
        rf"(?:{'|'.join(_GUANGYA_SCHEDULE_ENABLE_VERBS)})\s*{target_pattern}|"
        rf"{target_pattern}\s*(?:{'|'.join(_GUANGYA_SCHEDULE_ENABLE_VERBS)})",
        message,
    ))
    disabled = bool(re.search(
        rf"(?:{'|'.join(_GUANGYA_SCHEDULE_DISABLE_VERBS)})\s*{target_pattern}|"
        rf"{target_pattern}\s*(?:{'|'.join(_GUANGYA_SCHEDULE_DISABLE_VERBS)})",
        message,
    ))
    if enabled and disabled:
        raise AgentToolError("光鸭定时整理策略包含互相冲突的开关动作")
    if enabled:
        return True
    if disabled:
        return False
    return None


def guangya_organize_schedule_policy_request(message: str) -> dict[str, Any] | None:
    """只识别明确、肯定且边界固定的光鸭定时整理策略修改。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if not normalized or "整理" not in normalized:
        return None
    if not any(scope in normalized for scope in ("光鸭", "云盘", "网盘")):
        return None
    if any(token in normalized for token in _GUANGYA_SCHEDULE_REJECT_TOKENS):
        return None

    arguments: dict[str, Any] = {}
    notification_target = r"(?:(?:光鸭|云盘|网盘)\s*)?(?:定时)?整理(?:任务)?\s*通知"
    notify_enabled = _guangya_schedule_toggle_value(normalized, notification_target)
    if notify_enabled is not None:
        arguments["notify_enabled"] = notify_enabled

    schedule_target = (
        r"(?:(?:光鸭|云盘|网盘)\s*)?"
        r"(?:自动整理|定时整理|整理定时任务|整理计划任务|整理调度|整理计划)"
    )
    without_notification = re.sub(
        rf"(?:{'|'.join(_GUANGYA_SCHEDULE_ENABLE_VERBS + _GUANGYA_SCHEDULE_DISABLE_VERBS)})"
        rf"\s*{notification_target}|{notification_target}\s*"
        rf"(?:{'|'.join(_GUANGYA_SCHEDULE_ENABLE_VERBS + _GUANGYA_SCHEDULE_DISABLE_VERBS)})",
        "",
        normalized,
    )
    enabled = _guangya_schedule_toggle_value(without_notification, schedule_target)
    if enabled is not None:
        arguments["enabled"] = enabled

    explicit_cron = re.search(
        r"(?:cron|调度表达式|计划表达式)(?:\s*表达式)?\s*(?:设为|设置为|改为|改成|[:：])\s*"
        r"([*0-9,/?L#W-]+(?:\s+[*0-9,/?L#W-]+){4})",
        normalized,
    )
    if explicit_cron:
        arguments["cron"] = " ".join(explicit_cron.group(1).strip().split())
    else:
        daily_time = re.search(
            r"(?:改为|改成|设置为|设为)\s*(?:每天|每日)\s*"
            r"([0-9]{1,2})(?:(?:\s*[:：]\s*([0-9]{1,2}))|"
            r"(?:\s*(?:点|时)(?:\s*([0-9]{1,2})\s*分?)?))",
            normalized,
        )
        if daily_time:
            hour = int(daily_time.group(1))
            minute = int(daily_time.group(2) or daily_time.group(3) or 0)
            if not 0 <= hour <= 23:
                raise AgentToolError("光鸭每日整理小时必须为 0 到 23")
            if not 0 <= minute <= 59:
                raise AgentToolError("光鸭每日整理分钟必须为 0 到 59")
            arguments["cron"] = f"{minute} {hour} * * *"

    return arguments or None


def is_guangya_organize_schedule_policy_summary_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if not normalized:
        return False
    if not any(scope in normalized for scope in ("光鸭", "云盘", "网盘")):
        return False
    if "整理" not in normalized and not any(token in normalized for token in (
        "定时策略", "调度策略", "计划表达式", "调度表达式", "cron", "定时配置",
    )):
        return False
    if guangya_organize_schedule_policy_request(normalized) is not None:
        return False
    if any(token in normalized for token in ("运行状态", "整理状态", "进度", "失败", "清理", "立即")):
        return False
    return any(token in normalized for token in (
        "定时策略", "调度策略", "计划表达式", "调度表达式", "cron", "定时配置",
        "查看定时", "看看定时", "当前定时", "通知开关", "怎么配置", "如何配置",
    ))


def is_guangya_connection_status_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold().strip()
    if not normalized or "光鸭" not in normalized:
        return False
    if any(token in normalized for token in (
        "重新连接", "连接光鸭", "登录光鸭", "绑定光鸭", "解绑", "验证码", "发送验证码",
    )):
        return False
    has_account_scope = any(token in normalized for token in ("账号", "账户", "连接", "在线", "凭据"))
    has_status = any(token in normalized for token in (
        "状态", "正常", "可用", "有效", "能连", "连得上", "是否连接", "校验", "验证",
    ))
    return has_account_scope and has_status


def is_agent_job_cancel_message(message: str) -> bool:
    """识别用户主动要求停止本会话后台全库检查。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold()
    has_scope = any(token in normalized for token in (
        "全库检查", "全库巡检", "媒体库检查", "媒体库巡检",
        "缺集检查", "缺集巡检", "后台检查", "后台巡检",
    ))
    has_cancel = any(token in normalized for token in (
        "取消", "停止", "停掉", "不要查了", "不用查了", "结束任务",
    ))
    return has_scope and has_cancel


def is_agent_job_status_message(message: str) -> bool:
    """识别用户主动启动的持久化全库检查进度。

    自动/定时巡检及“上次结果”属于已存在的巡检历史查询，必须继续交给
    ``library.patrol_status``，不能被后台任务路由抢占。
    """
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold()
    if is_agent_job_cancel_message(normalized):
        return False
    if any(token in normalized for token in (
        "自动巡检", "自动缺集巡检", "定时巡检", "定时缺集巡检",
        "巡检策略", "巡检配置", "巡检周期", "上次", "最近",
    )):
        return False
    has_scope = any(token in normalized for token in (
        "全库检查", "全库巡检", "媒体库检查", "媒体库巡检",
        "缺集检查", "缺集巡检", "后台检查", "后台巡检",
    ))
    has_progress = any(token in normalized for token in (
        "进度", "到哪", "完成了吗", "查完了吗", "还在查",
        "跑完", "任务情况", "任务状态", "后台任务",
    ))
    return has_scope and has_progress


def is_library_patrol_status_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold()
    if _is_recent_library_patrol_resource_reference(normalized):
        return False
    if any(token in normalized for token in (
        "开启", "启用", "关闭", "停用", "配置", "设置",
        "周期", "间隔", "频率",
    )):
        return False
    if any(phrase in normalized for phrase in (
        "怎么开启", "如何开启", "怎么启用", "如何启用",
        "怎么关闭", "如何关闭", "怎么设置", "如何设置",
    )):
        return False
    has_patrol = any(token in normalized for token in (
        "缺集巡检", "全库巡检", "媒体库巡检", "自动巡检", "定时巡检",
    ))
    has_history = any(token in normalized for token in (
        "上次", "最近", "后台", "状态", "结果", "发现",
    ))
    return has_patrol and has_history


def _recent_patrol_selection(message: str) -> int | None:
    matched = re.search(
        r"第\s*([0-9]{1,2}|[一二两三四五六七八九十])\s*(?:个|项|条|部)",
        unicodedata.normalize("NFKC", str(message or "")),
    )
    if not matched:
        return None
    value = matched.group(1)
    if value.isdigit():
        return int(value)
    return _CHINESE_SELECTION_NUMBERS.get(value)


def is_library_episode_patrol_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold()
    if is_indexer_resource_search_message(normalized):
        return False
    if any(
        token in normalized
        for token in ("如何", "怎么配置", "配置", "设置", "功能", "支持什么", "什么格式", "说明")
    ):
        return False
    has_scope = any(token in normalized for token in _LIBRARY_EPISODE_PATROL_SCOPES)
    jellyfin_library_scope = (
        any(token in normalized for token in ("jellyfin", "emby", "媒体"))
        and "库" in normalized
        and any(token in normalized for token in ("所有剧", "全部剧", "全库", "整个库"))
    )
    has_specific_series_anchor = bool(
        re.search(r"[《「『\"']([^》」』\"']{1,120})[》」』\"']", message)
        or re.search(r"\btmdb\s*(?:id)?\s*[:：#]?\s*[0-9]{1,10}\b", normalized)
        or re.search(r"第\s*[0-9]{1,3}\s*季|\bS[0-9]{1,3}\b", normalized, re.IGNORECASE)
    )
    generic_library_scope = "媒体库" in normalized and not has_specific_series_anchor
    has_episode_focus = any(
        token in normalized
        for token in ("剧集", "电视剧", "番剧", "缺集", "漏集", "少集", "所有剧", "全部剧")
    ) or bool(re.search(r"(?:部|个)\s*剧", normalized))
    has_intent = any(token in normalized for token in _LIBRARY_EPISODE_PATROL_INTENTS)
    has_intent = has_intent or bool(re.search(r"(?<!多)少集", normalized))
    has_action = any(
        token in normalized
        for token in ("巡检", "体检", "检查", "核对", "扫描", "查一下", "看看", "有没有缺", "是否缺")
    )
    return (
        (has_scope or jellyfin_library_scope or generic_library_scope)
        and has_episode_focus
        and has_intent
        and has_action
    )


def is_explicit_background_library_episode_patrol_message(message: str) -> bool:
    """仅把明确的全量/后台语义送入可恢复任务，普通“查看媒体库”保持即时只读。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold()
    return any(token in normalized for token in (
        "全库", "整库", "整个媒体库", "全部媒体库", "媒体库所有剧",
        "所有剧集", "全部剧集", "全量", "完整巡检", "后台巡检",
        "后台检查", "分批检查", "分批巡检",
    ))


def _extract_library_episode_patrol_args(message: str) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    count = re.search(r"(?:前|最多|抽查)\s*([0-9]{1,3})\s*(?:部|个)?", message)
    if count:
        value = int(count.group(1))
        if 1 <= value <= 100:
            arguments["max_series"] = value
    as_of = re.search(r"(?<![0-9])([0-9]{4}-[0-9]{2}-[0-9]{2})(?![0-9])", message)
    if as_of:
        arguments["as_of"] = as_of.group(1)
    return arguments


def is_library_series_episode_count_message(message: str) -> bool:
    """识别“本地媒体库里某剧有多少集”，与缺集/更新审计严格分流。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold()
    if not _has_local_episode_count_scope(normalized):
        return False
    if any(token in normalized for token in _LOCAL_EPISODE_COUNT_REJECT_TOKENS):
        return False
    if re.search(r"第\s*(?:多少|几)\s*集", normalized):
        return False
    return bool(
        re.search(
            r"(?:(?:一共|总共|共有|共|收录)\s*)?(?:有\s*)?(?:多少|几)\s*集",
            normalized,
        )
        or re.search(r"集数\s*(?:是|为)?\s*(?:多少|几)", normalized)
    )


_GENERIC_LIBRARY_SCOPE_NAMES = {
    "", "媒体", "媒体库", "本地", "本地库", "我的", "我的库", "库",
    "jellyfin", "emby",
}


_NAMED_LIBRARY_SCOPE_PATTERNS = (
    r"我的\s*(?P<name>[^，。！？?、:：\s]{1,40}?库)\s*(?:中|里)",
    r"(?:在|从)\s*(?P<name>[^，。！？?、:：\s]{1,40}?库)\s*(?:中|里)",
)


def _extract_named_library_scope(message: str) -> str:
    """提取完整的用户可见库名，例如“美女库”或“儿童媒体库”。"""
    normalized = unicodedata.normalize("NFKC", str(message or ""))
    for pattern in _NAMED_LIBRARY_SCOPE_PATTERNS:
        matched = re.search(pattern, normalized, re.IGNORECASE)
        if not matched:
            continue
        name = matched.group("name").strip(" ，。！？?、:：")
        name = re.sub(r"^(?:我的|这个|该)", "", name).strip()
        if name.casefold() in _GENERIC_LIBRARY_SCOPE_NAMES:
            continue
        if name and not any(unicodedata.category(char).startswith("C") for char in name):
            return name[:40]
    return ""


def _strip_named_library_scope(message: str) -> str:
    cleaned = unicodedata.normalize("NFKC", str(message or ""))
    for pattern in _NAMED_LIBRARY_SCOPE_PATTERNS:
        cleaned = re.sub(pattern, " ", cleaned, count=1, flags=re.IGNORECASE)
    return cleaned


def _has_local_episode_count_scope(message: str) -> bool:
    """接受固定媒体库叫法，也接受用户给媒体库起的自定义名称。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold()
    if any(scope in normalized for scope in _LOCAL_EPISODE_COUNT_SCOPES):
        return True
    return bool(re.search(
        r"(?:我的|在|从)\s*[\u4e00-\u9fffA-Za-z0-9_-]{1,38}库\s*(?:中|里)",
        normalized,
    ))


def is_library_series_episode_count_and_audit_message(message: str) -> bool:
    """识别“一共有多少集，并检查是否缺集”这类一次完成的复合请求。"""
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold()
    if not _has_local_episode_count_scope(normalized):
        return False
    has_count = bool(
        re.search(
            r"(?:(?:一共|总共|共有|共|收录)\s*)?(?:有\s*)?(?:多少|几)\s*集",
            normalized,
        )
        or re.search(r"集数\s*(?:是|为)?\s*(?:多少|几)", normalized)
    )
    has_audit = any(token in normalized for token in _EPISODE_AUDIT_TOKENS)
    return has_count and has_audit


def _extract_library_series_episode_count_args(message: str) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    library_name = _extract_named_library_scope(message)
    if library_name:
        arguments["library_name"] = library_name
    quoted = re.search(r"[《「『\"']([^》」』\"']{1,120})[》」』\"']", message)
    if quoted:
        arguments["query"] = quoted.group(1).strip()

    tmdb = re.search(
        r"\btmdb\s*(?:id)?\s*[:：#]?\s*([0-9]{1,10})\b",
        message,
        re.IGNORECASE,
    )
    if tmdb:
        arguments["tmdb_id"] = tmdb.group(1)

    if "query" in arguments:
        return arguments

    cleaned = re.sub(
        r"\btmdb\s*(?:id)?\s*[:：#]?\s*[0-9]{1,10}\b",
        "",
        unicodedata.normalize("NFKC", str(message or "")),
        flags=re.IGNORECASE,
    )
    if library_name:
        cleaned = _strip_named_library_scope(cleaned)
    scope = r"(?:(?:我的)?(?:媒体库|本地库|我的库|库里|jellyfin|emby)\s*(?:中|里)?)"
    count_suffix = (
        r"(?:(?:一共|总共|共有|共|收录)\s*)?(?:有\s*)?(?:多少|几)\s*集"
        r"|集数\s*(?:是|为)?\s*(?:多少|几)"
    )
    patterns = []
    if library_name:
        patterns.append(
            rf"^(?:请|帮我)?(?:查看|查询|检查|看看|查一下|统计)?\s*(.+?)\s*(?:{count_suffix})(?:吗|呢)?$"
        )
    patterns.extend((
        rf"^(?:请|帮我)?(?:查看|查询|检查|看看|查一下|统计)?\s*(?:在\s*)?{scope}\s*(.+?)\s*(?:{count_suffix})(?:吗|呢)?$",
        rf"^(?:请|帮我)?(?:查看|查询|检查|看看|查一下|统计)?\s*(.+?)\s*(?:在\s*)?{scope}\s*(?:{count_suffix})(?:吗|呢)?$",
    ))
    for pattern in patterns:
        matched = re.search(pattern, cleaned, re.IGNORECASE)
        if not matched:
            continue
        query = matched.group(1).strip(" ，。！？?、:：")
        query = re.sub(
            r"^(?:请|帮我|查看|查询|检查|看看|查一下|统计)\s*",
            "",
            query,
            flags=re.IGNORECASE,
        ).strip(" ，。！？?、:：")
        if query:
            arguments["query"] = query[:120]
            break
    return arguments


def is_episode_audit_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold()
    if is_indexer_resource_search_message(normalized):
        return False
    if is_library_series_episode_count_message(normalized):
        return False
    if any(token in normalized for token in _EPISODE_AUDIT_TOKENS):
        return True
    if re.search(r"(?<!多)少集", normalized):
        return True
    has_audit_verb = any(token in normalized for token in ("核对", "检查"))
    has_season_scope = _season_coordinate(normalized) is not None
    return has_audit_verb and has_season_scope


def _extract_library_update_args(message: str) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    quoted = re.search(r"[《「『\"']([^》」』\"']{1,120})[》」』\"']", message)
    if quoted:
        arguments["query"] = quoted.group(1).strip()

    tmdb = re.search(r"\btmdb\s*(?:id)?\s*[:：#]?\s*([0-9]{1,10})\b", message, re.IGNORECASE)
    if tmdb:
        arguments["tmdb_id"] = tmdb.group(1)

    season = re.search(r"第\s*([0-9]{1,3})\s*季", message)
    if not season:
        season = re.search(r"\bS([0-9]{1,3})\b", message, re.IGNORECASE)
    if season:
        arguments["season"] = int(season.group(1))

    scope_text = re.sub(r"[《「『\"'].*?[》」』\"']", "", message)
    normalized_scope = unicodedata.normalize("NFKC", scope_text).casefold()
    media_prefixes = ("电视剧", "剧集", "番剧", "动漫", "动画", "电影", "影片", "片子")
    if any(token in normalized_scope for token in ("电影", "影片", "片子")):
        arguments["media_type"] = "movie"
    elif (
        any(token in normalized_scope for token in ("电视剧", "剧集", "番剧", "动漫", "动画", "新一集", "新一季"))
        or season is not None
    ):
        arguments["media_type"] = "tv"
    else:
        arguments["media_type"] = "auto"

    if "query" not in arguments:
        cleaned = re.sub(r"\btmdb\s*(?:id)?\s*[:：#]?\s*[0-9]{1,10}\b", "", message, flags=re.IGNORECASE)
        cleaned = re.sub(r"第\s*[0-9]{1,3}\s*季|\bS[0-9]{1,3}\b", "", cleaned, flags=re.IGNORECASE)
        patterns = (
            r"(?:检查|核对|看看|查一下)?\s*(.+?)\s*(?:有没有|有无|是否有|是否)\s*(?:更新|新一集|新一季|新内容)(?:吗)?",
            r"(?:检查|核对|看看|查一下)?\s*(.+?)\s*有\s*(?:新一集|新一季|新内容)(?:吗)?",
            r"(?:检查|核对|看看|查一下)?\s*(.+?)\s*(?:更新了吗|有更新吗)",
        )
        for pattern in patterns:
            matched = re.search(pattern, cleaned, re.IGNORECASE)
            if not matched:
                continue
            query = matched.group(1).strip(" ，。！？?、:：")
            query = re.sub(r"^(?:帮我|请|检查|核对|看看|查一下)\s*", "", query).strip()
            query = re.sub(rf"^(?:{'|'.join(media_prefixes)})\s*", "", query).strip()
            if query and not _is_generic_media_collection_term(query):
                arguments["query"] = query[:120]
                break
    return arguments


def is_library_update_check_message(message: str) -> bool:
    normalized = unicodedata.normalize("NFKC", str(message or "")).casefold()
    if not any(token in normalized for token in _LIBRARY_UPDATE_TOKENS):
        return False
    if is_workspace_search_message(normalized):
        return False
    if is_indexer_resource_search_message(normalized):
        return False
    external_scope = any(token in normalized for token in ("网上", "外部", "影视探索", "探索页", "豆瓣", "bangumi", "bgm"))
    search_verbs = r"(?:搜索|查询|查找|找|搜|查)"
    tmdb_search = bool(re.search(
        rf"(?:(?:用\s*)?tmdb\s*{search_verbs}|{search_verbs}\s*(?:一下\s*)?tmdb)",
        normalized,
    ))
    if external_scope or tmdb_search:
        return False
    if any(token in normalized for token in _LIBRARY_UPDATE_REJECT_TOKENS):
        return False
    arguments = _extract_library_update_args(message)
    query = str(arguments.get("query") or "")
    if not query:
        return False
    quoted = bool(re.search(r"[《「『\"'][^》」』\"']+[》」』\"']", message))
    media_scoped = arguments.get("media_type") != "auto" or bool(arguments.get("tmdb_id") or arguments.get("season"))
    if not quoted and not media_scoped:
        if len(query) > 40 or any(token in query.casefold() for token in _LIBRARY_UPDATE_NON_MEDIA_TOKENS):
            return False
    return True


def _extract_episode_audit_args(message: str) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    library_name = _extract_named_library_scope(message)
    if library_name:
        arguments["library_name"] = library_name
    quoted = re.search(r"[《「『\"']([^》」』\"']{1,120})[》」』\"']", message)
    if quoted:
        arguments["query"] = quoted.group(1).strip()

    tmdb = re.search(r"\btmdb\s*(?:id)?\s*[:：#]?\s*([0-9]{1,10})\b", message, re.IGNORECASE)
    if tmdb:
        arguments["tmdb_id"] = tmdb.group(1)

    coordinates = _episode_coordinates(message)
    if coordinates is not None:
        arguments["season"], arguments["target_episode"] = coordinates
    else:
        season = _season_coordinate(message)
        if season is not None:
            arguments["season"] = season

    as_of = re.search(
        r"(?<![0-9])([0-9]{4}-[0-9]{2}-[0-9]{2})(?![0-9])",
        message,
    )
    if as_of:
        arguments["as_of"] = as_of.group(1)

    if "query" not in arguments:
        cleaned = _strip_named_library_scope(message)
        cleaned = re.sub(r"\btmdb\s*(?:id)?\s*[:：#]?\s*[0-9]{1,10}\b", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(
            rf"第\s*{_HUMAN_NUMBER_TOKEN_RE}\s*季\s*第\s*{_HUMAN_NUMBER_TOKEN_RE}\s*集"
            rf"|(?<![A-Za-z0-9])S[0-9]{{1,3}}\s*E[0-9]{{1,4}}(?![A-Za-z0-9])"
            rf"|(?<![A-Za-z0-9])[0-9]{{1,3}}x[0-9]{{1,4}}(?![A-Za-z0-9])"
            rf"|第\s*{_HUMAN_NUMBER_TOKEN_RE}\s*季|\bS[0-9]{{1,3}}\b",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        patterns = (
            r"(?:检查|核对|看看|查一下)?\s*(.+?)\s*(?:有没有|有无|是否)?\s*(?:缺集|(?<!多)少集|漏集|更新|新一集|新一季|完整性)",
            r"(.+?)\s*(?:有没有|有无|是否)\s*(?:更新|缺集|(?<!多)少集|漏集)",
        )
        for pattern in patterns:
            matched = re.search(pattern, cleaned, re.IGNORECASE)
            if matched:
                query = matched.group(1).strip(" ，。！？?、:：")
                query = re.sub(r"^(?:帮我|请|检查|核对|看看|查一下)\s*", "", query).strip()
                if (
                    query
                    and query not in {"有没有", "有无", "是否", "检查", "核对", "看看", "查一下"}
                    and not _is_generic_media_collection_term(query)
                ):
                    arguments["query"] = query[:120]
                    break
    return arguments


def _clean_library_search_query(value: str) -> str:
    query = str(value or "").strip(" ，。！？?、:：")
    query = re.sub(r"(?:吗|呢)$", "", query).strip(" ，。！？?、:：")
    query = re.sub(
        r"(?:这部|这套)?(?:电影|电视剧|剧集|动画|动漫|影片|片子)$",
        "",
        query,
        flags=re.IGNORECASE,
    ).strip(" ，。！？?、:：")
    if _is_generic_media_collection_term(query):
        return ""
    return query[:120]


def _extract_search_query(message: str) -> str:
    quoted = re.search(r"[《「『\"']([^》」』\"']{1,120})[》」』\"']", message)
    if quoted:
        return quoted.group(1).strip()

    local_scope = r"(?:媒体库|本地库|我的库|库里|jellyfin|emby)\s*(?:中|里)?"
    patterns = (
        rf"(?:在\s*)?{local_scope}\s*(?:是否存在|是否有|有没有|有无|有)\s*(.+?)(?:吗|呢)?$",
        rf"(?:在\s*)?{local_scope}\s*(?:能不能|能否|能|可以|可不可以)?\s*(?:找到|找得到|搜到|查到|检索到)\s*(.+?)(?:吗|呢)?$",
        rf"(?:在\s*)?{local_scope}\s*(.+?)\s*(?:是否存在|在不在)(?:吗|呢)?$",
        rf"(.+?)\s*(?:在不在|是否在|存在于|在)\s*{local_scope}\s*(?:吗|呢)?$",
        rf"(.+?)\s*(?:是否存在|在不在)\s*(?:吗|呢)?$",
        r"(?:帮我)?(?:找一下|找找|搜索|查找|找(?!到|得到)|搜(?!索))\s*(?:一下\s*)?(?:电影|电视剧|剧集|动画|动漫|影片|片子)?\s*[:：]?\s*(.+)",
        r"(?:片名|剧名)\s*[:：]\s*(.+)",
        r"(?:媒体库里)?(?:有没有|有无)\s*(.+)",
    )
    for pattern in patterns:
        matched = re.search(pattern, message, flags=re.IGNORECASE)
        if matched:
            query = _clean_library_search_query(matched.group(1))
            if query:
                return query
    return ""


def _is_strm_run_action(message: str) -> bool:
    normalized = _normalize_intent_message(message)
    if "strm" not in normalized or _is_dangerous_action_discussion(normalized):
        return False
    if any(token in normalized for token in ("状态", "进度", "怎么样", "完成了吗", "到哪")):
        return False
    return (
        "同步" in normalized
        and any(token in normalized for token in ("立即", "开始", "启动", "执行", "手动", "运行一次"))
    )


def _has_organize_scope(message: str) -> bool:
    normalized = str(message or "").casefold()
    return "整理" in normalized and any(token in normalized for token in ("光鸭", "云盘", "网盘"))


def is_guangya_organize_preview_message(message: str) -> bool:
    normalized = str(message or "").casefold()
    return _has_organize_scope(normalized) and any(
        token in normalized for token in ("预览", "计划", "会怎么整理", "先看看", "试运行")
    )


def is_guangya_organize_stop_message(message: str) -> bool:
    normalized = str(message or "").casefold()
    if not _has_organize_scope(normalized):
        return False
    if any(
        token in normalized
        for token in (
            "不要", "别", "不许", "无需", "不用", "能不能", "能否", "可以吗", "是否",
            "怎么", "如何", "为什么", "会不会", "停止后", "取消后", "然后", "再启动",
            "重新整理", "继续整理", "停止了吗", "停止了没", "取消了吗", "取消了没",
            "定时", "自动",
        )
    ):
        return False
    return any(token in normalized for token in ("停止", "取消", "中止", "终止"))


def is_guangya_organize_clean_empty_message(message: str) -> bool:
    normalized = str(message or "").casefold()
    has_scope = any(
        token in normalized
        for token in ("光鸭整理", "云盘整理", "网盘整理", "整理来源", "来源目录", "源目录")
    )
    if not has_scope:
        return False
    if any(
        token in normalized
        for token in (
            "不要", "别", "不许", "无需", "不用", "能不能", "能否", "可以吗", "是否",
            "怎么", "怎样", "如何", "为什么", "会不会", "有没有", "什么", "命令",
            "查询", "查看", "记录", "结果", "已经", "清理了吗", "删除了吗", "定时", "自动",
            "预览", "计划", "状态", "进度", "停止", "取消", "中止", "终止",
            "风险", "恢复", "影响", "后果", "注意事项", "安全吗", "安全性", "可逆",
        )
    ) or "?" in normalized or "？" in normalized or normalized.rstrip().endswith(("吗", "呢")):
        return False
    has_action = (
        any(token in normalized for token in ("清理", "删除"))
        and any(token in normalized for token in ("空目录", "空文件夹"))
    )
    has_explicit_command = any(
        token in normalized
        for token in (
            "立即", "开始", "执行", "帮我", "请", "现在", "马上", "清理一下", "删除一下",
        )
    ) or normalized.rstrip().endswith(("吧", "掉"))
    # 保留控制台中既有的短命令形式，但拒绝任何带咨询语义的长句自动进入危险确认。
    concise_command = has_action and len(normalized.strip()) <= 16
    return has_action and (has_explicit_command or concise_command)


def is_guangya_organize_run_message(message: str) -> bool:
    normalized = _normalize_intent_message(message)
    if not _has_organize_scope(normalized) or _is_dangerous_action_discussion(normalized):
        return False
    if any(
        token in normalized
        for token in ("状态", "进度", "到哪", "完成了吗", "预览", "计划", "定时", "停止", "取消", "中止", "终止")
    ):
        return False
    return any(
        token in normalized
        for token in ("立即", "开始", "启动", "执行", "运行一次", "跑一次", "整理一下", "帮我整理", "请整理")
    )


class AgentOrchestrator:
    def __init__(self, registry: ToolRegistry, confirmation_store: ConfirmationStore | None = None,
                 *, recent_patrol_store: RecentPatrolStore | None = None,
                 recent_resource_store: RecentResourceCandidateStore | None = None,
                 recent_discovery_store: RecentDiscoveryCandidateStore | None = None,
                 recent_download_store: RecentDownloadSubmissionStore | None = None,
                 recent_read_store: RecentReadOperationStore | None = None,
                 missing_workflow_repository: MissingMediaWorkflowRepository | None = None,
                 session_context_repository: AgentSessionContextRepository | None = None,
                 automatic_verification_enqueuer: (
                     Callable[[ToolResult, dict[str, Any] | None, str], bool] | None
                 ) = None,
                 record_actions: bool = False):
        self.registry = registry
        self.confirmation_store = confirmation_store or ConfirmationStore()
        self.recent_patrol_store = recent_patrol_store or RecentPatrolStore()
        self.recent_resource_store = recent_resource_store or RecentResourceCandidateStore()
        self.recent_discovery_store = recent_discovery_store or RecentDiscoveryCandidateStore()
        self.recent_download_store = recent_download_store or RecentDownloadSubmissionStore()
        self.recent_read_store = recent_read_store or RecentReadOperationStore()
        self.missing_workflow_repository = missing_workflow_repository
        self.session_context_repository = session_context_repository
        self.automatic_verification_enqueuer = automatic_verification_enqueuer
        self.record_actions = bool(record_actions)

    def begin_query_confirmation_epoch(self, *, owner: str) -> int:
        """为一次 latest-wins query 捕获不可复用的确认 epoch。"""
        owner_key = str(owner or "").strip()
        if not owner_key:
            raise AgentToolError("当前会话无法创建确认请求", code="confirmation_invalid")
        _revoked, generation = self.confirmation_store.rotate_owner(owner=owner_key)
        return generation

    def invalidate_query_confirmation_epoch(self, *, owner: str) -> int:
        """撤销后台旧 query 已创建或尚未创建的确认票据。"""
        owner_key = str(owner or "").strip()
        if not owner_key:
            return 0
        revoked, _generation = self.confirmation_store.rotate_owner(owner=owner_key)
        return revoked

    def reset_session(self, *, owner: str) -> dict[str, Any]:
        """撤销指定会话的短期票据与续接上下文。"""
        owner_key = str(owner or "").strip()
        if not owner_key:
            raise AgentToolError("当前会话无法重置", code="confirmation_invalid")
        revoked = self.confirmation_store.revoke_owner(owner=owner_key)
        self.recent_patrol_store.clear_owner(owner=owner_key)
        self.recent_resource_store.clear_owner(owner=owner_key)
        self.recent_discovery_store.clear_owner(owner=owner_key)
        self.recent_download_store.clear_owner(owner=owner_key)
        self.recent_read_store.clear_owner(owner=owner_key)
        persisted = 0
        if self.session_context_repository is not None:
            try:
                persisted = self.session_context_repository.delete_owner(owner=owner_key)
            except Exception as exc:
                logger.warning("Agent 会话持久化上下文清理失败 type=%s", type(exc).__name__)
                raise AgentToolError("会话重置暂时无法完成，请稍后重试") from exc
        return {
            "reset": True,
            "revoked_confirmations": revoked,
            "deleted_contexts": persisted,
        }

    def discard_confirmation(self, confirmation_id: str, *, owner: str) -> bool:
        return self.confirmation_store.discard(
            owner=owner,
            confirmation_id=confirmation_id,
        )

    def capabilities(self) -> dict[str, Any]:
        return {"tools": self.registry.capabilities(), "mode": "confirmation_gated"}

    def has_tool(self, tool_name: str) -> bool:
        """在创建动态限流键之前确认工具属于受控注册表。"""
        return self.registry.has(tool_name)

    def invoke(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        owner: str = "",
    ) -> dict[str, Any]:
        normalized_arguments = arguments or {}
        if isinstance(self.registry, ToolRegistry):
            # 最近重试必须保存 handler 实际收到的默认值/规范化参数；同时保留
            # 轻量测试 registry 仅实现既有 execute() 契约的兼容性。
            normalized_arguments = self.registry.validate_read_call(
                tool_name, normalized_arguments
            )
        result, elapsed_ms = self.registry.execute(
            tool_name,
            arguments,
            context=ToolContext(owner=owner),
        )
        if tool_name in {
            "library.audit_library_episodes",
            "library.patrol_status",
        } and owner:
            self.recent_patrol_store.capture(owner=owner, result=result)
        if (
            tool_name == "agent.job_status"
            and owner
            and result.status in {"updates_available", "up_to_date", "inconclusive"}
        ):
            self.recent_patrol_store.capture(owner=owner, result=result)
        if tool_name in {
            "library.search_missing_episode_resources",
            "library.search_missing_season_resources",
            "indexer.search_resources",
            "media.subscription_updates",
        } and owner:
            self.recent_resource_store.capture(owner=owner, result=result)
        if tool_name in {"discovery.search", "discovery.recommend"} and owner:
            self.recent_discovery_store.capture(owner=owner, result=result)
        if (
            tool_name in {
                "library.search_missing_episode_resources",
                "library.search_missing_season_resources",
            }
            and owner
            and self.missing_workflow_repository is not None
        ):
            try:
                self.missing_workflow_repository.capture_search(
                    owner=owner,
                    tool_name=tool_name,
                    result=result,
                )
            except Exception as exc:
                # 搜索结果仍然有效；持久进度镜像失败不能篡改只读工具响应。
                logger.warning(
                    "Agent 补库工作流记录搜索失败 type=%s",
                    type(exc).__name__,
                )
        if owner:
            # 只记录显式白名单中的幂等只读调用，供“重试/再查一次”安全续接。
            self.recent_read_store.capture(
                owner=owner, tool_name=tool_name, arguments=normalized_arguments
            )
        return self._response(tool_name, normalized_arguments, result, elapsed_ms)

    @staticmethod
    def _reserve_query_tool_budget(tool_name: str, *, rate_identity: str = "") -> None:
        """让自然语言入口和直接工具调用按真实工具共享同一份预算。"""
        effective_rate_identity = str(
            rate_identity or _QUERY_TOOL_RATE_IDENTITY.get() or ""
        ).strip()
        if (
            effective_rate_identity
            and not allow_agent_tool(effective_rate_identity, tool_name)
        ):
            raise AgentToolError(
                "Agent 请求过于频繁，请稍后重试",
                code="rate_limited",
            )

    def _invoke_query_read(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        owner: str = "",
        rate_identity: str = "",
    ) -> dict[str, Any]:
        """执行自然语言查询命中的只读工具，并与直调接口共享工具预算。"""
        # ToolRegistry.execute/invoke 已执行最终风险门；这里仅统一自然语言入口的预算。
        self._reserve_query_tool_budget(tool_name, rate_identity=rate_identity)
        return self.invoke(tool_name, arguments or {}, owner=owner)

    def _execute_read_plan(
        self,
        plan: LLMReadPlan,
        *,
        owner: str = "",
        rate_identity: str = "",
    ) -> dict[str, Any]:
        if not 2 <= len(plan.steps) <= 4:
            raise AgentToolError("只读计划步骤数量无效", code="precondition_failed")

        normalized_steps: list[tuple[str, dict[str, Any]]] = []
        seen_names: set[str] = set()
        for step in plan.steps:
            tool_name = str(step.tool_name or "").strip()
            if not tool_name or tool_name in seen_names:
                raise AgentToolError("只读计划包含重复或无效工具", code="precondition_failed")
            if self.registry.risk_for(tool_name) is not RiskLevel.READ:
                raise AgentToolError("只读计划包含非只读工具", code="precondition_failed")
            normalized = self.registry.validate_read_call(tool_name, step.arguments)
            seen_names.add(tool_name)
            normalized_steps.append((tool_name, normalized))

        if rate_identity:
            for tool_name, _ in normalized_steps:
                if not allow_agent_tool(rate_identity, tool_name):
                    raise AgentToolError(
                        "Agent 请求过于频繁，请稍后重试", code="rate_limited"
                    )

        public_steps: list[dict[str, Any]] = []
        total_elapsed_ms = 0
        completed = 0
        suggestions: list[str] = []
        for position, (tool_name, arguments) in enumerate(normalized_steps, start=1):
            response = self.invoke(tool_name, arguments, owner=owner)
            tool_call = response.get("tool_call") if isinstance(response, dict) else None
            elapsed_ms = (
                int(tool_call.get("elapsed_ms") or 0)
                if isinstance(tool_call, dict)
                else 0
            )
            total_elapsed_ms += max(0, elapsed_ms)
            result = response.get("result") if isinstance(response, dict) else None
            if not isinstance(result, dict):
                result = ToolResult(
                    False,
                    "unavailable",
                    "工具返回了无效结果",
                    error="该步骤暂时无法读取。",
                ).to_dict()
            if result.get("ok") is True:
                completed += 1
            for item in result.get("suggestions", []):
                text = " ".join(str(item or "").split())[:240]
                if text and text not in suggestions:
                    suggestions.append(text)
            public_steps.append({
                "position": position,
                "tool_name": tool_name,
                "elapsed_ms": max(0, elapsed_ms),
                "result": result,
            })

        failed = len(public_steps) - completed
        aggregate = ToolResult(
            ok=failed == 0,
            status="completed" if failed == 0 else "partial",
            summary=(
                f"复合检查完成：{completed} 项正常"
                if failed == 0
                else f"复合检查完成：{completed} 项正常，{failed} 项需要关注"
            ),
            data={
                "step_count": len(public_steps),
                "completed": completed,
                "failed": failed,
                "steps": public_steps,
            },
            suggestions=suggestions[:8],
            error=("部分检查未能正常完成。" if failed else ""),
        )
        response = self._response(
            "agent.read_plan",
            {"step_count": len(public_steps)},
            aggregate,
            total_elapsed_ms,
            mode="read_plan",
        )
        if owner and not self.recent_read_store.capture_plan(
            owner=owner,
            steps=normalized_steps,
        ):
            # 内部 invoke 会逐步覆盖最近操作；若整份计划不满足安全重放条件，
            # 必须清除最后一步，避免用户以为“重试”会重新执行完整检查。
            self.recent_read_store.clear_owner(owner=owner)
        return response

    def _replay_recent_read(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        owner: str,
        rate_identity: str,
    ) -> dict[str, Any]:
        """重放最近一次安全只读调用；复合检查保持原子语义。"""
        if tool_name == READ_PLAN_OPERATION:
            raw_steps = arguments.get("steps")
            if not isinstance(raw_steps, list):
                raise AgentToolError("最近复合检查已失效", code="precondition_failed")
            selections: list[LLMToolSelection] = []
            for item in raw_steps:
                if not isinstance(item, dict):
                    raise AgentToolError("最近复合检查已失效", code="precondition_failed")
                replay_name = str(item.get("tool_name") or "").strip()
                replay_arguments = item.get("arguments")
                if not replay_name or not isinstance(replay_arguments, dict):
                    raise AgentToolError("最近复合检查已失效", code="precondition_failed")
                if replay_name == "library.audit_episodes":
                    invalidate_episode_audit_cache(replay_arguments)
                selections.append(LLMToolSelection(replay_name, replay_arguments))
            return self._execute_read_plan(
                LLMReadPlan(tuple(selections)),
                owner=owner,
                rate_identity=rate_identity,
            )

        if tool_name == "library.audit_episodes":
            # “重试/再查一次”应重新读取媒体服务器与 TMDB，而不是命中 120 秒缓存。
            invalidate_episode_audit_cache(arguments)
        return self._invoke_query_read(
            tool_name,
            arguments,
            owner=owner,
            rate_identity=rate_identity,
        )

    def invoke_workspace_action(
        self,
        action_key: str,
        *,
        owner: str = "",
        rate_identity: str = "",
    ) -> dict[str, Any]:
        resolution = resolve_workspace_action_handoff({"action_key": action_key})
        target_tool = str(resolution["target_tool"])
        if self.registry.risk_for(target_tool) is not RiskLevel.READ:
            raise AgentToolError(
                "工作区行动目标不是只读工具",
                code="precondition_failed",
            )
        if rate_identity and not allow_agent_tool(rate_identity, target_tool):
            raise AgentToolError(
                "Agent 请求过于频繁，请稍后重试",
                code="rate_limited",
            )
        return self.invoke(target_tool, {}, owner=owner)

    def _continue_recent_library_patrol(self, message: str, *, owner: str) -> dict[str, Any]:
        snapshot = self.recent_patrol_store.get(owner=owner)
        options = snapshot.get("options", []) if isinstance(snapshot, dict) else []
        if not isinstance(options, list) or not options:
            return self._response(
                "library.audit_library_episodes",
                {},
                ToolResult(
                    False,
                    "precondition_failed",
                    "当前会话没有可继续的全库巡检结果",
                    suggestions=["请先执行：巡检整个媒体库有没有缺集。"],
                    error="最近巡检结果不存在、已过期或没有可靠的缺集候选。",
                ),
                0,
            )

        selection = _recent_patrol_selection(message)
        if len(options) > 1 and selection is None:
            return self._patrol_selection_response(snapshot, "巡检发现多个缺集季度，请先选择一项")
        if selection is None:
            selection = 1
        if selection < 1 or selection > len(options):
            return self._patrol_selection_response(snapshot, f"巡检结果中没有第 {selection} 项")

        selected = options[selection - 1]
        arguments = {
            "query": selected["title"],
            "tmdb_id": selected["tmdb_id"],
            "season": selected["season"],
            "max_episodes": 3,
            "limit_per_episode": 8,
        }
        as_of = str(snapshot.get("as_of") or "")
        if as_of:
            arguments["as_of"] = as_of
        return self._invoke_query_read(
            "library.search_missing_season_resources", arguments, owner=owner
        )

    def _continue_recent_resource_submit(
        self, request: dict[str, Any], *, owner: str
    ) -> dict[str, Any]:
        if not owner:
            return self._unsupported(
                "该动作需要在已登录会话中确认",
                ["请重新登录后先搜索缺集资源，再明确选择推荐项和下载目标。"],
            )
        snapshot = self.recent_resource_store.get(owner=owner)
        candidates = snapshot.get("candidates", []) if isinstance(snapshot, dict) else []
        if not isinstance(candidates, list) or not candidates:
            return self._response(
                "indexer.submit_resource",
                {},
                ToolResult(
                    False,
                    "precondition_failed",
                    "当前会话没有可提交的最近资源推荐",
                    suggestions=["请先搜索某个已确认缺失的单集或季度资源。"],
                    error="最近资源推荐不存在、已过期或没有可安全提交的候选。",
                ),
                0,
            )

        position = request.get("position")
        target = request.get("target")
        episode = request.get("episode")
        if not isinstance(position, int) and isinstance(episode, int):
            matching_positions = [
                index
                for index, candidate in enumerate(candidates, start=1)
                if _candidate_episode(candidate) == episode
            ]
            if len(matching_positions) == 1:
                position = matching_positions[0]
            elif len(matching_positions) > 1:
                return self._resource_selection_response(
                    snapshot,
                    f"第 {episode} 集有多个候选，请回复候选序号和下载目标。",
                    target=target if isinstance(target, str) else "",
                )
            else:
                return self._resource_selection_response(
                    snapshot,
                    f"最近资源推荐中没有识别到第 {episode} 集的候选。",
                    target=target if isinstance(target, str) else "",
                )
        if not isinstance(position, int):
            return self._resource_selection_response(
                snapshot,
                "想下载第几个？回复序号和目标即可。",
                target=target if isinstance(target, str) else "",
            )
        if position < 1 or position > len(candidates):
            return self._resource_selection_response(
                snapshot,
                f"最近资源推荐中没有第 {position} 项",
                target=target if isinstance(target, str) else "",
            )
        if target not in {"qb", "guangya", "both"}:
            return self._resource_selection_response(
                snapshot,
                f"第 {position} 个要下到哪里？请选择 qB、光鸭或两边。",
                position=position,
            )

        selected = candidates[position - 1]
        verification_context = (
            selected.get("_verification_context")
            if isinstance(selected.get("_verification_context"), dict)
            else None
        )
        workflow_ref = None
        if (
            verification_context is not None
            and self.missing_workflow_repository is not None
        ):
            try:
                workflow_ref = self.missing_workflow_repository.select_candidate(
                    owner=owner,
                    verification=verification_context,
                    candidate_title=str(selected.get("title") or ""),
                    target=target,
                )
            except Exception as exc:
                logger.warning(
                    "Agent 补库工作流选择候选失败 type=%s",
                    type(exc).__name__,
                )
        return self.prepare(
            "indexer.submit_resource",
            {"result_id": selected["result_id"], "target": target},
            owner=owner,
            followup_context=workflow_followup_context(
                verification_context,
                workflow_ref,
            ),
        )

    def _continue_recent_download_status(self, message: str, *, owner: str) -> dict[str, Any]:
        request = recent_download_status_request(message) or {}
        if not owner:
            return self._unsupported(
                "最近任务状态只对已登录会话开放",
                ["请重新登录后，在同一会话中提交资源并查询状态。"],
            )
        records = self.recent_download_store.get(owner=owner)
        if not records:
            return self._response(
                "downloads.recent_submission_status",
                {},
                ToolResult(
                    False,
                    "precondition_failed",
                    "当前会话没有可查询的最近资源提交",
                    suggestions=["请先搜索资源、确认提交，然后再问：刚才下载到哪了。"],
                    error="最近提交记录不存在、已过期，或本地会话上下文不可用。",
                ),
                0,
            )
        position = request.get("position")
        if position is None:
            position = 1
        if not isinstance(position, int) or position < 1 or position > len(records):
            return self._response(
                "downloads.recent_submission_status",
                {},
                ToolResult(
                    False,
                    "selection_required",
                    f"最近提交记录中没有第 {position} 项",
                    data={"available_positions": list(range(1, len(records) + 1))},
                    suggestions=[
                        f"回复：第 {index} 个任务状态。"
                        for index in range(1, min(len(records), 5) + 1)
                    ],
                    error="需要选择当前会话中仍有效的最近提交序号。",
                ),
                0,
            )
        result = build_recent_download_status(records[position - 1], position=position)
        return self._response("downloads.recent_submission_status", {}, result, 0)

    def _continue_recent_download_library_verification(
        self,
        message: str,
        *,
        owner: str,
    ) -> dict[str, Any]:
        request = recent_download_library_verification_request(message) or {}
        tool_name = "downloads.verify_recent_submission_library"
        if not owner:
            return self._unsupported(
                "最近任务入库核验只对已登录会话开放",
                ["请重新登录后，在同一会话中提交缺集资源并再次核验。"],
            )
        records = self.recent_download_store.get(owner=owner)
        if not records:
            return self._response(
                tool_name, {},
                ToolResult(
                    False, "precondition_failed", "当前会话没有可核验的最近资源提交",
                    suggestions=["请先从已确认缺失的季集搜索资源并完成提交。"],
                    error="最近提交记录不存在、已过期，或本地会话上下文不可用。",
                ), 0,
            )
        position = request.get("position") or 1
        if not isinstance(position, int) or position < 1 or position > len(records):
            return self._response(
                tool_name, {},
                ToolResult(
                    False, "selection_required", f"最近提交记录中没有第 {position} 项",
                    data={"available_positions": list(range(1, len(records) + 1))},
                    suggestions=[
                        f"回复：检查刚才下载的第 {index} 个任务是否已入库。"
                        for index in range(1, min(len(records), 5) + 1)
                    ],
                    error="需要选择当前会话中仍有效的最近提交序号。",
                ), 0,
            )
        record = records[position - 1]
        if record.verification is None:
            result = build_recent_download_library_verification(
                record, ToolResult(False, "inconclusive", "缺少核验上下文"), position=position
            )
            return self._response(tool_name, {}, result, 0)
        status_result = build_recent_download_status(record, position=position)
        status_data = status_result.data if isinstance(status_result.data, dict) else {}
        phase = str(status_data.get("phase") or "unknown")
        if phase != "completed":
            active = phase in {
                "pending", "submitting", "submitted", "downloading", "post_processing",
                "partial_in_progress", "accepted",
            }
            return self._response(
                tool_name, {},
                ToolResult(
                    False,
                    "in_progress" if active else "precondition_failed",
                    ("最近任务尚未完成下载与入库前处理" if active else "最近任务当前不能执行可靠的入库核验"),
                    data={"position": position, "phase": phase},
                    suggestions=(
                        ["可稍后再次询问：刚才下载的缺集是否已补齐。"]
                        if active else
                        ["可先询问：刚才下载为什么失败，或前往下载任务页人工核对。"]
                    ),
                    error="只有下载及后处理完成后，才会重新审计媒体库。",
                ), 0,
            )
        verification = record.verification
        arguments = {
            "query": verification.title,
            "tmdb_id": verification.tmdb_id,
            "season": verification.season,
            "target_episode": verification.episode,
            "as_of": verification.as_of,
        }
        if verification.library_name:
            arguments["library_name"] = verification.library_name
        invalidate_episode_audit_cache(arguments)
        audit, elapsed_ms = self.registry.execute("library.audit_episodes", arguments)
        result = build_recent_download_library_verification(record, audit, position=position)
        return self._response(tool_name, arguments, result, elapsed_ms)

    def _continue_recent_download_explanation(self, message: str, *, owner: str) -> dict[str, Any]:
        request = recent_download_explanation_request(message) or {}
        tool_name = "downloads.explain_recent_submission"
        if not owner:
            return self._unsupported(
                "最近任务异常解释只对已登录会话开放",
                ["请重新登录后，在同一会话中提交资源并查询异常原因。"],
            )
        records = self.recent_download_store.get(owner=owner)
        if not records:
            return self._response(
                tool_name,
                {},
                ToolResult(
                    False,
                    "precondition_failed",
                    "当前会话没有可解释的最近资源提交",
                    suggestions=["请先搜索资源、确认提交，然后再问：刚才下载为什么失败。"],
                    error="最近提交记录不存在、已过期，或本地会话上下文不可用。",
                ),
                0,
            )
        position = request.get("position")
        if position is None:
            position = 1
        if not isinstance(position, int) or position < 1 or position > len(records):
            return self._response(
                tool_name,
                {},
                ToolResult(
                    False,
                    "selection_required",
                    f"最近提交记录中没有第 {position} 项",
                    data={"available_positions": list(range(1, len(records) + 1))},
                    suggestions=[
                        f"回复：第 {index} 个任务为什么失败。"
                        for index in range(1, min(len(records), 5) + 1)
                    ],
                    error="需要选择当前会话中仍有效的最近提交序号。",
                ),
                0,
            )
        result = explain_recent_download_status(records[position - 1], position=position)
        return self._response(tool_name, {}, result, 0)

    @staticmethod
    def _resource_selection_response(
        snapshot: dict[str, Any],
        summary: str,
        *,
        position: int | None = None,
        target: str = "",
    ) -> dict[str, Any]:
        candidates = [
            public_candidate_projection(item)
            for item in snapshot.get("candidates", [])
            if isinstance(item, dict)
        ]
        suggestions: list[str] = []
        if position is None:
            target_label = {"qb": "qB", "guangya": "光鸭", "both": "两个后端"}.get(target, "qB")
            suggestions = [
                f"回复：第 {item['position']} 个到 {target_label}。"
                for item in candidates[:5]
            ]
        else:
            suggestions = [
                f"回复：第 {position} 个到 qB。",
                f"回复：第 {position} 个到光鸭。",
                f"回复：第 {position} 个到两边。",
            ]
        return AgentOrchestrator._response(
            "indexer.submit_resource",
            {},
            ToolResult(
                False,
                "selection_required",
                summary,
                data={"candidates": candidates},
                suggestions=suggestions,
                error="需要明确选择候选序号和提交目标后才能创建确认请求。",
            ),
            0,
        )

    @staticmethod
    def _patrol_selection_response(snapshot: dict[str, Any], summary: str) -> dict[str, Any]:
        options = list(snapshot.get("options", []))
        suggestions = [
            f"回复：把刚才巡检第 {item['position']} 项缺集找资源。"
            for item in options[:5]
        ]
        return AgentOrchestrator._response(
            "library.audit_library_episodes",
            {},
            ToolResult(
                False,
                "selection_required",
                summary,
                data={
                    "as_of": snapshot.get("as_of", ""),
                    "findings_truncated": bool(snapshot.get("findings_truncated")),
                    "options": options,
                },
                suggestions=suggestions,
                error="需要明确选择一个巡检候选后才能继续搜索。",
            ),
            0,
        )

    def prepare(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        *,
        owner: str,
        followup_context: dict[str, Any] | None = None,
        expected_owner_generation: int | None = None,
    ) -> dict[str, Any]:
        query_epoch = _QUERY_CONFIRMATION_EPOCH.get()
        if (
            expected_owner_generation is None
            and query_epoch is not None
            and secrets.compare_digest(query_epoch[0], str(owner or "").strip())
        ):
            expected_owner_generation = query_epoch[1]
        owner_generation = (
            self.confirmation_store.owner_generation(owner=owner)
            if expected_owner_generation is None
            else int(expected_owner_generation)
        )
        spec, normalized, context, preview, elapsed_ms = self.registry.prepare_confirmation(
            tool_name,
            arguments,
            context=ToolContext(owner=owner),
        )
        confirmation_contract = build_confirmation_contract(
            tool_name=spec.name,
            risk=spec.risk,
            preview=preview,
        )
        ticket = self.confirmation_store.issue(
            owner=owner,
            tool_name=spec.name,
            arguments=normalized,
            context_fingerprint=context,
            followup_context=followup_context,
            confirmation_contract=confirmation_contract,
            expected_owner_generation=owner_generation,
        )
        response = {
            "request_id": secrets.token_urlsafe(12),
            "mode": "confirmation_required",
            "tool_call": {"name": spec.name, "elapsed_ms": elapsed_ms},
            "result": preview.to_dict(),
            "confirmation": {
                "confirmation_id": ticket.confirmation_id,
                "tool": spec.name,
                "risk": spec.risk.value,
                "expires_in": self.confirmation_store.ttl_seconds,
                "contract": sanitize_confirmation_contract(ticket.confirmation_contract),
            },
        }
        guidance = result_projection.project_public_guidance(preview.suggestions)
        if guidance:
            response["guidance"] = guidance
        return result_projection.attach_public_display(response)

    @staticmethod
    def _enabled_rss_subscriptions() -> list[dict[str, Any]]:
        return [
            item
            for item in db.list_enabled_rss_subscription_safe_targets()
            if int(item.get("subscription_number") or 0) > 0
        ]

    def _prepare_enabled_rss_refresh(self, *, owner: str) -> dict[str, Any]:
        """为当前全部已启用订阅创建一次批量刷新确认。"""
        if not owner:
            return self._unsupported(
                "刷新 RSS 订阅需要在已登录会话中确认",
                ["请登录后重试。"],
            )
        if not self._enabled_rss_subscriptions():
            return self._unsupported(
                "当前没有已启用的 RSS 订阅",
                ["可先列出全部 RSS 订阅，确认名称和启用状态。"],
            )
        return self.prepare(
            "rss.refresh_subscriptions",
            {"scope": "all_enabled"},
            owner=owner,
        )

    def _prepare_contextual_rss_refresh(self, *, owner: str) -> dict[str, Any]:
        """刷新意图没有名称时，只有唯一启用订阅才自动补全。"""
        enabled = self._enabled_rss_subscriptions()
        if not enabled:
            return self._unsupported(
                "当前没有已启用的 RSS 订阅",
                ["可先列出全部 RSS 订阅，确认名称和启用状态。"],
            )
        if len(enabled) == 1:
            subscription_id = int(enabled[0].get("subscription_number") or 0)
            if not owner:
                return self._unsupported(
                    "刷新 RSS 订阅需要在已登录会话中确认",
                    ["请登录后重试。"],
                )
            return self.prepare(
                "rss.refresh_subscription",
                {"subscription_id": subscription_id},
                owner=owner,
            )
        names = [
            str(item.get("name") or f"#{item.get('subscription_number')}").strip()
            for item in enabled[:5]
        ]
        return self._clarification_response(
            "有多个已启用的 RSS 订阅，请告诉我要刷新哪一个，或明确说“刷新全部 RSS 订阅”。",
            [f"刷新 {name} RSS 订阅。" for name in names],
        )

    def _rss_context_followup(
        self,
        message: str,
        *,
        owner: str,
        conversation_context: list[dict[str, Any]] | None,
    ) -> dict[str, Any] | None:
        """让“列出列表 / 刷新一下”在刚讨论 RSS 时保持同一主题。"""
        previous = _latest_assistant_tool_context(conversation_context)
        previous_tool = str(previous.get("tool_name") or "").strip()
        if not previous_tool.startswith("rss."):
            return None
        normalized = re.sub(
            r"[\s，。！？!?、；;：:~～]+",
            "",
            unicodedata.normalize("NFKC", str(message or "")).casefold(),
        )
        if normalized in {
            "列出列表", "列出全部", "列出全部列表", "全部列表", "列表", "都有哪些", "有哪些",
        }:
            return self._invoke_query_read("rss.subscription_summaries", {}, owner=owner)
        if normalized in {"近24小时下载几次", "近24小时下载了几次", "24小时下载几次"}:
            return self._invoke_query_read("rss.recent_activity", {}, owner=owner)
        if normalized in {"全部刷新", "刷新全部", "刷新全部rss订阅", "全部rss订阅刷新"}:
            return self._prepare_enabled_rss_refresh(owner=owner)
        if normalized in {"刷新", "刷新一下", "再刷新", "再刷新一下", "继续刷新"}:
            return self._prepare_contextual_rss_refresh(owner=owner)
        named = re.fullmatch(r"刷新(?:一下)?(.+?)(?:rss)?(?:订阅)?", normalized)
        if named:
            name = named.group(1).strip()
            resolution = resolve_rss_subscription_name(name)
            if resolution.status == "resolved" and resolution.subscription_id is not None:
                if not owner:
                    return self._unsupported(
                        "刷新 RSS 订阅需要在已登录会话中确认",
                        ["请登录后重试。"],
                    )
                return self.prepare(
                    "rss.refresh_subscription",
                    {"subscription_id": resolution.subscription_id},
                    owner=owner,
                )
        return None

    def confirm(self, confirmation_id: str, *, owner: str) -> dict[str, Any]:
        ticket = self.confirmation_store.claim(owner=owner, confirmation_id=confirmation_id)
        risk = self.registry.risk_for(ticket.tool_name)
        try:
            result, elapsed_ms = self.registry.execute_confirmed(
                ticket.tool_name,
                ticket.arguments,
                expected_context=ticket.context_fingerprint,
                context=ToolContext(owner=owner),
            )
        except AgentToolError as exc:
            if self.record_actions:
                record_confirmation_error(
                    owner=owner,
                    tool_name=ticket.tool_name,
                    risk=risk,
                    code=exc.code,
                    confirmation_contract=ticket.confirmation_contract,
                )
            raise
        if ticket.tool_name == "indexer.submit_resource":
            self.recent_download_store.capture(
                owner=owner,
                result=result,
                verification_context=ticket.followup_context,
            )
            if self.automatic_verification_enqueuer is not None:
                try:
                    self.automatic_verification_enqueuer(
                        result,
                        ticket.followup_context,
                        owner,
                    )
                except Exception as exc:
                    # 下载已经执行成功；自动复核排队失败不能篡改确认结果。
                    logger.warning(
                        "Agent 下载后媒体库自动复核排队失败 type=%s",
                        type(exc).__name__,
                    )
        if self.record_actions:
            record_confirmed_result(
                owner=owner,
                tool_name=ticket.tool_name,
                risk=risk,
                result=result,
                elapsed_ms=elapsed_ms,
                confirmation_contract=ticket.confirmation_contract,
            )
        public_result = (
            sanitize_submission_confirmation_result(result)
            if ticket.tool_name == "indexer.submit_resource"
            else result
        )
        if (
            ticket.tool_name == "indexer.submit_resource"
            and public_result.ok
            and public_result.status == "accepted"
            and parse_recent_download_verification_context(
                ticket.followup_context
            ) is not None
        ):
            public_result.suggestions.append(
                "可询问：刚才下载的缺集入库完成了吗。"
            )
        if (
            ticket.tool_name == "config.set_indexer_sites"
            and public_result.ok
            and ticket.followup_context.get("kind") == "indexer_search_after_config"
        ):
            title = str(ticket.followup_context.get("title") or "").strip()
            limit = max(1, min(int(ticket.followup_context.get("limit") or 20), 50))
            result_data = public_result.data if isinstance(public_result.data, dict) else {}
            runtime_refresh = result_data.get("runtime_refresh")
            web_runtime_ready = not isinstance(runtime_refresh, dict) or runtime_refresh.get("web") is not False
            if title and web_runtime_ready:
                followup = self.invoke(
                    "indexer.search_resources",
                    {"title": title, "limit": limit},
                    owner=owner,
                )
                followup_result = followup.get("result")
                if isinstance(followup_result, dict):
                    followup_result["summary"] = (
                        f"{public_result.summary}。{str(followup_result.get('summary') or '').strip()}"
                    ).strip("。")
                    followup["mode"] = "confirmed_action"
                    return result_projection.attach_public_display(followup)
            if title and not web_runtime_ready:
                suggestion = (
                    f"站点选择已经保存，但当前 Web 搜索服务尚未刷新；"
                    f"重启当前服务后再搜索《{title}》。"
                )
                if suggestion not in public_result.suggestions:
                    public_result.suggestions.append(suggestion)
        return self._response(
            ticket.tool_name,
            {},
            public_result,
            elapsed_ms,
            mode="confirmed_action",
        )

    def query(
        self,
        value: Any,
        *,
        owner: str = "",
        llm_rate_owner: str = "",
        query_tool_rate_identity: str = "",
        llm_tool_rate_identity: str = "",
        conversation_context: list[dict[str, Any]] | None = None,
        reply_context: dict[str, Any] | None = None,
        present: bool = True,
        confirmation_owner_generation: int | None = None,
    ) -> dict[str, Any]:
        message = normalize_agent_message(value)
        confirmation_token = None
        rate_identity_token = None
        owner_key = str(owner or "").strip()
        if confirmation_owner_generation is not None and owner_key:
            confirmation_token = _QUERY_CONFIRMATION_EPOCH.set(
                (owner_key, int(confirmation_owner_generation))
            )
        query_rate_identity = str(query_tool_rate_identity or "").strip()
        if query_rate_identity:
            rate_identity_token = _QUERY_TOOL_RATE_IDENTITY.set(query_rate_identity)
        try:
            contextual_feature_request = feature_state_followup_request(
                message, conversation_context
            )
            if contextual_feature_request is not None:
                response = (
                    self.prepare(
                        "config.set_feature_state",
                        contextual_feature_request,
                        owner=owner,
                    )
                    if owner
                    else self._unsupported(
                        "该配置动作需要在已登录会话中确认",
                        ["请通过 Agent 页面重新提交，并在预检后确认修改。"],
                    )
                )
                if not present:
                    return response
                return self._present_tool_response(
                    message, response, owner=llm_rate_owner or owner
                )
            rss_contextual = self._rss_context_followup(
                message,
                owner=owner,
                conversation_context=conversation_context,
            )
            if rss_contextual is not None:
                if not present:
                    return rss_contextual
                return self._present_tool_response(
                    message, rss_contextual, owner=llm_rate_owner or owner
                )
            rating_request = contextual_media_rating_request(
                message, conversation_context
            )
            if rating_request is not None and self.registry.has("discovery.lookup_rating"):
                response = self._invoke_query_read(
                    "discovery.lookup_rating",
                    rating_request,
                    owner=owner,
                    rate_identity=query_tool_rate_identity,
                )
                if not present:
                    return response
                return self._present_tool_response(
                    message, response, owner=llm_rate_owner or owner
                )
            if is_recent_read_retry_message(message):
                if not owner:
                    response = self._unsupported(
                        "当前会话没有可安全重试的查询",
                        ["请重新描述要查询的目标。"],
                    )
                else:
                    recent_read = self.recent_read_store.get(owner=owner)
                    if recent_read is None:
                        response = self._unsupported(
                            "当前会话没有可安全重试的查询",
                            ["请重新描述要查询的目标。"],
                        )
                    else:
                        tool_name, arguments = recent_read
                        response = self._replay_recent_read(
                            tool_name,
                            arguments,
                            owner=owner,
                            rate_identity=query_tool_rate_identity,
                        )
                if not present:
                    return response
                return self._present_tool_response(
                    message, response, owner=llm_rate_owner or owner
                )
            local_conversation = self._local_conversation(message)
            if local_conversation is not None:
                return local_conversation
            continued = self._continue_narrow_followup(
                message,
                owner=llm_rate_owner or owner,
                conversation_context=conversation_context,
                reply_context=reply_context,
            )
            if continued is not None:
                return continued
            clarification = self._clarify_ambiguous_followup(
                message,
                conversation_context=conversation_context,
                reply_context=reply_context,
            )
            if clarification is not None:
                return clarification
            # 最近资源候选属于已经建立的强上下文。像“第 2 个到光鸭”或
            # “下载 34 集到 qB”必须先进入确认流程，不能再次交给模型猜测。
            if owner and self.recent_resource_store.get(owner=owner) is not None:
                recent_resource_request = _recent_resource_pre_model_submit_request(
                    message, conversation_context
                )
                if recent_resource_request is not None:
                    response = self._continue_recent_resource_submit(
                        recent_resource_request, owner=owner
                    )
                    if not present:
                        return response
                    return self._present_tool_response(
                        message, response, owner=llm_rate_owner or owner
                    )
            # 专用业务路由必须先于模型工具选择。这样下载诊断、RSS 控制、
            # 缺集审计等确定性请求不会被模型误选成其他工具；只有全部专用路由
            # 都未命中时，_query_raw 才会在末尾调用受注册表约束的模型工具层。
            response = self._query_raw(
                message,
                owner=owner,
                llm_rate_owner=llm_rate_owner,
                query_tool_rate_identity=query_tool_rate_identity,
                llm_tool_rate_identity=llm_tool_rate_identity,
                conversation_context=conversation_context,
                allow_model_routing=True,
            )
            if not present:
                return response
            return self._present_tool_response(
                message, response, owner=llm_rate_owner or owner
            )
        finally:
            if rate_identity_token is not None:
                _QUERY_TOOL_RATE_IDENTITY.reset(rate_identity_token)
            if confirmation_token is not None:
                _QUERY_CONFIRMATION_EPOCH.reset(confirmation_token)

    def _query_with_model_tools(
        self,
        message: str,
        *,
        owner: str,
        llm_rate_owner: str,
        llm_tool_rate_identity: str,
        conversation_context: list[dict[str, Any]] | None,
        read_only: bool = False,
    ) -> dict[str, Any] | None:
        """让模型在注册表边界内理解请求，失败时返回 ``None`` 继续确定性回退。

        普通查询优先进入原生多工具只读循环，使模型可以围绕同一目标连续读取多个
        数据源并自行归纳。``read_only`` 模式不会向模型暴露确认型工具；明确动作
        仍只能通过既有统一选择器创建确认票据。非法名称、越权风险或参数错误均
        按失败关闭处理。
        """
        rate_identity = llm_tool_rate_identity or llm_rate_owner or owner
        action_request = is_agent_action_request(message)

        def _execute_native_read(
            tool_name: str, arguments: dict[str, Any]
        ) -> dict[str, Any]:
            if rate_identity and not allow_agent_tool(rate_identity, tool_name):
                raise AgentToolError(
                    "Agent 请求过于频繁，请稍后重试", code="rate_limited"
                )
            return self.invoke(tool_name, arguments, owner=owner)

        def _execute_selection(selection: Any) -> dict[str, Any] | None:
            if selection is None:
                return None
            try:
                disposition, normalized_arguments = (
                    self.registry.validate_llm_orchestration_call(
                        selection.tool_name, selection.arguments
                    )
                )
            except AgentToolError:
                # 模型输出不在注册表暴露面或参数不合法时严格回退，绝不执行。
                return None
            if disposition is LLMToolDisposition.PREPARE_CONFIRMATION:
                if not owner:
                    return None
                return self.prepare(
                    selection.tool_name,
                    normalized_arguments,
                    owner=owner,
                )
            if disposition is LLMToolDisposition.EXECUTE_READ:
                if rate_identity and not allow_agent_tool(
                    rate_identity, selection.tool_name
                ):
                    raise AgentToolError(
                        "Agent 请求过于频繁，请稍后重试", code="rate_limited"
                    )
                return self.invoke(
                    selection.tool_name, normalized_arguments, owner=owner
                )
            return None

        native_reply = None
        if not action_request:
            native_reply = run_native_read_agent(
                message,
                self.registry,
                _execute_native_read,
                owner=llm_rate_owner or owner,
                **(
                    {"conversation_context": conversation_context}
                    if conversation_context else {}
                ),
            )

        if native_reply is None:
            # 复合只读请求交给后面的 JSON 计划回退，避免在原生循环失败后退化成
            # 只执行一个工具；明确动作和普通单目标请求可继续尝试统一选择器。
            if not action_request and is_compound_read_request(message):
                return None

            if not read_only:
                selection = select_orchestration_tool(
                    message,
                    self.registry,
                    owner=owner,
                    rate_owner=llm_rate_owner or owner,
                    **(
                        {"conversation_context": conversation_context}
                        if conversation_context else {}
                    ),
                )
                selected_response = _execute_selection(selection)
                if selected_response is not None:
                    return selected_response

            # 明确动作但统一路由无法可靠匹配时，交回针对候选对象、确认票据和
            # 业务上下文的确定性解析器；不能把“刷新/下载/修改”当成普通对话。
            if action_request:
                return None

            # 单工具兼容回退仍必须经过注册表白名单和参数 schema 校验。
            selection = select_read_tool(
                message,
                self.registry,
                owner=llm_rate_owner or owner,
                **(
                    {"conversation_context": conversation_context}
                    if conversation_context else {}
                ),
            )
            if selection is None:
                return None
            allowed_names = {
                str(item.get("name") or "").strip()
                for item in self.registry.llm_read_capabilities()
                if isinstance(item, dict)
            }
            if selection.tool_name not in allowed_names:
                return None
            try:
                normalized_arguments = self.registry.validate_read_call(
                    selection.tool_name, selection.arguments
                )
            except AgentToolError:
                return None
            if rate_identity and not allow_agent_tool(rate_identity, selection.tool_name):
                raise AgentToolError(
                    "Agent 请求过于频繁，请稍后重试", code="rate_limited"
                )
            return self.invoke(selection.tool_name, normalized_arguments, owner=owner)

        trace = [dict(item) for item in native_reply.tool_trace]
        executions = [
            dict(item) for item in native_reply.tool_executions
            if isinstance(item, dict)
        ]
        # 关键边界：模型只负责解释和编排，真实工具响应继续作为响应主体。
        # 不能把它降级成 tool_call=None 的普通对话，否则候选资源、媒体身份、
        # RSS 订阅对象和后续确认入口都会在这一层丢失。
        response: dict[str, Any] | None = None
        if executions:
            last_execution = executions[-1]
            raw_response = last_execution.get("response")
            if isinstance(raw_response, dict):
                response = deepcopy(raw_response)

        if response is None:
            response = {
                "request_id": secrets.token_urlsafe(12),
                "mode": "conversation",
                "tool_call": None,
                "result": ToolResult(
                    ok=native_reply.completed,
                    status="answered" if native_reply.completed else "partial",
                    summary=native_reply.answer,
                    data=(
                        {"complete": native_reply.completed, "checks": trace}
                        if trace else {}
                    ),
                    suggestions=list(native_reply.suggestions),
                    error=(
                        "部分检查已完成，但后续分析暂时中断。"
                        if not native_reply.completed else ""
                    ),
                ).to_dict(),
            }

        presentation = result_projection.build_public_narrative_presentation(
            native_reply.answer,
            native_reply.suggestions,
        )
        if presentation:
            response["presentation"] = presentation
        if trace:
            response["agent_trace"] = trace
        if not native_reply.completed:
            response["agent_partial"] = {
                "complete": False,
                "reason": result_projection.sanitize_public_text(
                    native_reply.stop_reason or "analysis_interrupted", limit=80
                ),
            }
        guidance = result_projection.project_public_guidance(
            native_reply.suggestions, force_kind="draft"
        )
        if guidance:
            response["guidance"] = guidance
        return result_projection.attach_public_display(response)

    def _handle_discovery_followup(
        self,
        message: str,
        *,
        owner: str,
        query_tool_rate_identity: str,
    ) -> dict[str, Any] | None:
        """处理最近探索结果与探索收藏的确定性续句。"""
        recent_discovery_request = recent_discovery_candidate_request(message)
        if recent_discovery_request is not None:
            if not owner:
                return self._unsupported(
                    "最近探索结果只在当前已登录会话中可用",
                    ["请在 Agent 页面重新搜索影片后继续操作。"],
                )
            snapshot = self.recent_discovery_store.get(owner=owner)
            candidates = snapshot.get("candidates", []) if isinstance(snapshot, dict) else []
            position = int(recent_discovery_request["position"])
            candidate = next((
                item for item in candidates
                if isinstance(item, dict) and int(item.get("position") or 0) == position
            ), None)
            if candidate is None:
                return self._clarification_response(
                    "没有找到对应的最近探索结果，结果可能已经过期或序号超出范围。",
                    ["重新搜索片名", "列出探索收藏"],
                )
            if recent_discovery_request["action"] == "watchlist_add":
                return self.prepare(
                    "discovery.add_watchlist",
                    {
                        "provider": candidate["provider"],
                        "external_id": candidate["external_id"],
                        "media_type": candidate["media_type"],
                    },
                    owner=owner,
                )
            if query_tool_rate_identity and not allow_agent_tool(
                query_tool_rate_identity, "indexer.search_resources"
            ):
                raise AgentToolError(
                    "Agent 请求过于频繁，请稍后重试", code="rate_limited"
                )
            return self.invoke(
                "indexer.search_resources",
                {"title": candidate["title"], "limit": 20},
                owner=owner,
            )

        watchlist_remove_request = discovery_watchlist_remove_request(message)
        if watchlist_remove_request is not None:
            if not owner:
                return self._unsupported(
                    "移除探索收藏需要在已登录会话中确认",
                    ["请通过 Agent 页面重新提交，并在预检后确认移除。"],
                )
            return self.prepare(
                "discovery.remove_watchlist", watchlist_remove_request, owner=owner
            )

        watchlist_summary_request = discovery_watchlist_summary_request(message)
        if watchlist_summary_request is not None:
            tool_name = (
                "discovery.get_watchlist_summary"
                if "watchlist_number" in watchlist_summary_request
                else "discovery.watchlist_summaries"
            )
            return self._invoke_query_read(tool_name, watchlist_summary_request, owner=owner)

        if is_discovery_watchlist_write_message(message):
            return self._clarification_response(
                "探索收藏一次只能操作一个明确对象。加入收藏请引用最近搜索结果序号；移除收藏请提供精确收藏编号。",
                [
                    "把刚才搜索结果第 2 项加入探索收藏",
                    "列出探索收藏",
                    "移除探索收藏编号 3",
                ],
            )
        return None

    def _handle_recent_resource_download_followup(
        self,
        message: str,
        *,
        owner: str,
    ) -> dict[str, Any] | None:
        """处理最近资源选择、下载状态、异常解释与入库核验续句。"""
        recent_resource_request = recent_resource_submit_request(message)
        if recent_resource_request is not None:
            return self._continue_recent_resource_submit(
                recent_resource_request, owner=owner
            )

        recent_library_verification = recent_download_library_verification_request(message)
        if recent_library_verification is not None:
            return self._continue_recent_download_library_verification(message, owner=owner)

        recent_explanation_request = recent_download_explanation_request(message)
        if recent_explanation_request is not None:
            return self._continue_recent_download_explanation(message, owner=owner)

        recent_status_request = recent_download_status_request(message)
        if recent_status_request is not None:
            return self._continue_recent_download_status(message, owner=owner)
        return None

    def _handle_local_media_requests(
        self,
        message: str,
        *,
        owner: str,
        query_tool_rate_identity: str,
    ) -> dict[str, Any] | None:
        """处理本地媒体来源控制、来源摘要与任务摘要请求。"""
        local_source_control = local_media_intents.local_media_source_trigger_control_request(message)
        if local_source_control is not None:
            tool_name, arguments = local_source_control
            if not owner:
                return self._unsupported(
                    "本地媒体来源触发器启停需要在已登录会话中确认",
                    ["请通过 Agent 页面重新提交，并在预检后确认修改。"],
                )
            return self.prepare(tool_name, arguments, owner=owner)
        if local_media_intents.is_local_media_source_trigger_control_message(message):
            return self._clarification_response(
                "请明确一个本地媒体来源编号，以及要调整 qB 下载完成自动接管还是目录自动扫描；Agent 不会猜测目标或批量修改。",
                [
                    "暂停本地媒体来源 2 的 qB 下载完成自动接管",
                    "启用本地媒体来源 2 的目录自动扫描",
                    "查看本地媒体来源 2 详情",
                ],
            )
        local_source_summary = local_media_intents.local_media_source_summary_request(message)
        if local_source_summary is not None:
            return self._invoke_query_read(
                "local_media.get_source_summary", local_source_summary
            )
        if local_media_intents.is_local_media_source_summaries_message(message):
            return self._invoke_query_read("local_media.source_summaries", {})
        if local_media_intents.is_local_media_review_queue_summary_message(message):
            return self._invoke_query_read(
                "local_media.review_queue_summary",
                owner=owner,
                rate_identity=query_tool_rate_identity,
            )
        if local_media_intents.is_local_media_history_summary_message(message):
            return self._invoke_query_read(
                "local_media.history_summary",
                owner=owner,
                rate_identity=query_tool_rate_identity,
            )
        return None

    def _handle_patrol_and_schedule_requests(
        self,
        message: str,
        *,
        lower: str,
        owner: str,
    ) -> dict[str, Any] | None:
        """处理媒体库巡检、后台检查及 STRM/光鸭调度策略。"""
        if is_recent_library_patrol_resource_message(lower):
            return self._continue_recent_library_patrol(message, owner=owner)
        if _is_recent_library_patrol_resource_reference(lower):
            return self._unsupported(
                "已取消最近巡检结果的资源接力",
                ["如需继续，请明确回复：把刚才巡检发现的缺集找资源。"],
            )
        patrol_policy_request = library_patrol_policy_request(lower)
        if patrol_policy_request is not None:
            if not owner:
                return self._unsupported(
                    "该巡检策略动作需要在已登录会话中确认",
                    ["请通过 Agent 页面重新提交，并在预检后确认修改。"],
                )
            return self.prepare(
                "library.set_patrol_policy",
                patrol_policy_request,
                owner=owner,
            )
        if is_library_patrol_policy_summary_message(lower):
            return self._invoke_query_read("library.patrol_policy", {})
        if is_agent_job_cancel_message(lower):
            if not owner:
                return self._unsupported(
                    "取消后台检查需要在已登录会话中确认",
                    ["请通过 Agent 页面重新提交，并在预检后确认取消。"],
                )
            return self.prepare("agent.cancel_job", {}, owner=owner)
        if is_agent_job_status_message(lower):
            if not owner:
                return self._unsupported(
                    "查询后台检查进度需要已登录会话",
                    ["请通过 Agent 页面重新提交。"],
                )
            return self._invoke_query_read("agent.job_status", {}, owner=owner)
        if is_library_patrol_status_message(lower):
            return self._invoke_query_read("library.patrol_status", {}, owner=owner)

        strm_schedule_request = strm_schedule_policy_request(message)
        if strm_schedule_request is not None:
            if not owner:
                return self._unsupported(
                    "该 STRM 调度策略动作需要在已登录会话中确认",
                    ["请通过 Agent 页面重新提交，并在预检后确认修改。"],
                )
            return self.prepare(
                "strm.set_schedule_policy",
                strm_schedule_request,
                owner=owner,
            )
        if is_strm_schedule_policy_summary_message(message):
            return self._invoke_query_read("strm.schedule_policy", {})

        guangya_schedule_request = guangya_organize_schedule_policy_request(message)
        if guangya_schedule_request is not None:
            if not owner:
                return self._unsupported(
                    "该光鸭定时整理策略动作需要在已登录会话中确认",
                    ["请通过 Agent 页面重新提交，并在预检后确认修改。"],
                )
            return self.prepare(
                "guangya.organize.set_schedule_policy",
                guangya_schedule_request,
                owner=owner,
            )
        if is_guangya_organize_schedule_policy_summary_message(message):
            return self._invoke_query_read("guangya.organize.schedule_policy", {})
        if is_guangya_connection_status_message(message):
            return self._invoke_query_read("guangya.connection_status", {})

        return None

    def _handle_download_and_media_subscription_requests(
        self,
        message: str,
        *,
        lower: str,
        owner: str,
    ) -> dict[str, Any] | None:
        """处理下载任务控制与媒体追更订阅请求。"""
        download_retry = download_retry_submission_request(message)
        if download_retry is not None:
            if not owner:
                return self._unsupported(
                    "重新提交下载请求需要在已登录会话中确认",
                    ["请通过 Agent 页面重新提交，并在只读预检后确认执行。"],
                )
            return self.prepare(
                "downloads.retry_submission",
                download_retry,
                owner=owner,
            )
        if is_download_retry_submission_message(lower):
            return self._clarification_response(
                "请同时提供待处理记录编号和唯一目标；Agent 不会猜测记录或下载后端。",
                [
                    "例如：把下载请求 12 重新提交到 qBittorrent。",
                    "例如：重试下载待处理记录 12 到光鸭。",
                    "例如：重新提交下载请求 12 到 qBittorrent 与光鸭。",
                ],
            )

        download_control = download_task_control_request(message)
        if download_control is not None:
            if not owner:
                return self._unsupported(
                    "该动作需要在已登录会话中确认",
                    ["请通过 Agent 页面重新提交，并在只读预检后确认执行。"],
                )
            tool_name, arguments = download_control
            return self.prepare(tool_name, arguments, owner=owner)
        if is_download_task_control_message(lower):
            return self._unsupported(
                "请用书名号或引号提供一个完整任务名称；同名任务不会由 Agent 猜测选择。",
                ["例如：暂停下载任务《Example.Show.S01E01》。"],
            )

        media_summary_request = media_subscription_summary_request(message)
        if media_summary_request is not None:
            return self._invoke_query_read("media.get_subscription_summary", media_summary_request, owner=owner)
        if is_media_subscription_updates_message(message):
            return self._invoke_query_read("media.subscription_updates", {}, owner=owner)
        if is_media_subscription_summaries_message(message):
            return self._invoke_query_read("media.subscription_summaries", {}, owner=owner)

        media_control_request = media_subscription_control_request(message)
        if media_control_request is not None:
            if not owner:
                return self._unsupported(
                    "该动作需要在已登录会话中确认",
                    ["请通过 Agent 页面重新提交，并在只读预检后确认执行。"],
                )
            tool_name, arguments = media_control_request
            return self.prepare(tool_name, arguments, owner=owner)
        if is_media_subscription_control_write_message(message):
            return self._unsupported(
                "请明确提供一个媒体追更订阅编号和唯一操作；Agent 不会批量暂停或恢复订阅。",
                [
                    "例如：暂停媒体订阅 12。",
                    "例如：恢复追更订阅 12。",
                    "如不知道编号，可先说：列出全部媒体追更订阅。",
                ],
            )

        return None

    def _handle_rss_requests(
        self,
        message: str,
        *,
        lower: str,
        owner: str,
    ) -> dict[str, Any] | None:
        """处理 RSS 查询、控制、刷新、提交与失败重试请求。"""
        rss_summary_request = rss_subscription_summary_request(message)
        if rss_summary_request is not None:
            return self._invoke_query_read(
                "rss.get_subscription_summary", rss_summary_request, owner=owner
            )
        rss_summary_name = rss_subscription_summary_name(message)
        if rss_summary_name is not None:
            resolution = resolve_rss_subscription_name(rss_summary_name)
            if resolution.status == "resolved" and resolution.subscription_id is not None:
                return self._invoke_query_read(
                    "rss.get_subscription_summary",
                    {"subscription_id": resolution.subscription_id},
                    owner=owner,
                )
            if resolution.status == "ambiguous":
                return self._unsupported(
                    f"找到多个名为《{rss_summary_name}》的 RSS 订阅，请选择一个订阅编号。",
                    [f"查看 RSS 订阅 {sid} 状态。" for sid in resolution.candidate_ids[:3]],
                )
            return self._unsupported(
                f"没有找到名为《{rss_summary_name}》的 RSS 订阅。",
                ["可先说：列出全部 RSS 订阅。"],
            )
        if is_rss_recent_activity_message(message):
            return self._invoke_query_read("rss.recent_activity", {}, owner=owner)
        if is_rss_subscription_summaries_message(message):
            return self._invoke_query_read("rss.subscription_summaries", {}, owner=owner)

        rss_control_request = rss_subscription_control_request(message)
        if rss_control_request is not None:
            if not owner:
                return self._unsupported(
                    "该动作需要在已登录会话中确认",
                    ["请通过 Agent 页面重新提交，并在只读预检后确认执行。"],
                )
            tool_name, arguments = rss_control_request
            return self.prepare(tool_name, arguments, owner=owner)
        rss_named_control = rss_subscription_control_name_request(message)
        if rss_named_control is not None:
            tool_name, subscription_name, extra_arguments = rss_named_control
            resolution = resolve_rss_subscription_name(subscription_name)
            if resolution.status == "resolved" and resolution.subscription_id is not None:
                if not owner:
                    return self._unsupported(
                        "该动作需要在已登录会话中确认",
                        ["请通过 Agent 页面重新提交，并在只读预检后确认执行。"],
                    )
                arguments = {
                    "subscription_id": resolution.subscription_id,
                    **extra_arguments,
                }
                return self.prepare(tool_name, arguments, owner=owner)
            if tool_name == "rss.set_subscription_enabled":
                action = "启用" if extra_arguments.get("enabled") else "停用"
                examples = [
                    f"{action} RSS 订阅 {sid}。"
                    for sid in resolution.candidate_ids[:3]
                ]
            elif tool_name == "rss.set_refresh_interval":
                interval = int(extra_arguments.get("refresh_interval_minutes", 0) or 0)
                examples = [
                    f"将 RSS 订阅 {sid} 刷新周期设为 {interval} 分钟。"
                    for sid in resolution.candidate_ids[:3]
                ]
            else:
                examples = [
                    f"删除 RSS 订阅 {sid}。"
                    for sid in resolution.candidate_ids[:3]
                ]
            if resolution.status == "ambiguous":
                return self._unsupported(
                    f"找到多个名为《{subscription_name}》的 RSS 订阅，请选择一个订阅编号。",
                    examples,
                )
            return self._unsupported(
                f"没有找到名为《{subscription_name}》的 RSS 订阅。",
                ["可先说：列出全部 RSS 订阅。"],
            )
        if is_rss_subscription_control_write_message(lower):
            return self._unsupported(
                "请提供一个 RSS 订阅名称；只有存在同名订阅时才需要编号。",
                [
                    "例如：停用 Mikan RSS 订阅。",
                    "例如：将 Mikan RSS 订阅刷新周期设为 30 分钟。",
                ],
            )

        rss_refresh_request = rss_subscription_refresh_request(lower)
        if rss_refresh_request is not None:
            if not owner:
                return self._unsupported(
                    "该动作需要在已登录会话中确认",
                    ["请通过 Agent 页面重新提交，并在只读预检后确认执行。"],
                )
            return self.prepare("rss.refresh_subscription", rss_refresh_request, owner=owner)
        rss_refresh_name = rss_subscription_refresh_name(message)
        if rss_refresh_name is not None:
            resolution = resolve_rss_subscription_name(rss_refresh_name)
            if resolution.status == "resolved" and resolution.subscription_id is not None:
                if not owner:
                    return self._unsupported(
                        "该动作需要在已登录会话中确认",
                        ["请通过 Agent 页面重新提交，并在只读预检后确认执行。"],
                    )
                return self.prepare(
                    "rss.refresh_subscription",
                    {"subscription_id": resolution.subscription_id},
                    owner=owner,
                )
            if resolution.status == "ambiguous":
                examples = [
                    f"刷新 RSS 订阅 {subscription_id}。"
                    for subscription_id in resolution.candidate_ids[:3]
                ]
                return self._unsupported(
                    f"找到多个名为《{rss_refresh_name}》的 RSS 订阅，请选择一个订阅编号。",
                    examples,
                )
            return self._unsupported(
                f"没有找到名为《{rss_refresh_name}》的 RSS 订阅。",
                ["可先说：列出全部 RSS 订阅。"],
            )
        if is_rss_subscription_refresh_write_message(lower):
            compact_rss_refresh = re.sub(r"[\s，。！？!?、；;：:]+", "", lower)
            if "全部" in compact_rss_refresh or "所有" in compact_rss_refresh:
                return self._prepare_enabled_rss_refresh(owner=owner)
            return self._prepare_contextual_rss_refresh(owner=owner)

        rss_download_request = rss_pending_download_request(lower)
        if rss_download_request is not None:
            if not owner:
                return self._unsupported(
                    "该动作需要在已登录会话中确认",
                    ["请通过 Agent 页面重新提交，并在只读预检后确认执行。"],
                )
            return self.prepare("rss.submit_pending_to_qb", rss_download_request, owner=owner)
        if is_rss_pending_download_write_message(lower):
            return self._unsupported(
                "Agent 当前只支持确认后向 qBittorrent 提交 1 至 20 个待处理 RSS 条目。",
                ["例如：向 qBittorrent 提交 10 个待处理 RSS 条目。"],
            )

        rss_retry_request = rss_failure_retry_request(lower)
        if rss_retry_request is not None:
            if not owner:
                return self._unsupported(
                    "该动作需要在已登录会话中确认",
                    ["请通过 Agent 页面重新提交，并在只读预检后确认执行。"],
                )
            return self.prepare("rss.retry_failed_to_qb", rss_retry_request, owner=owner)
        if is_rss_failure_retry_write_message(lower):
            return self._unsupported(
                "Agent 当前只支持确认后重试 1 至 20 个已明确分类为可安全重试的 qB RSS 失败条目。",
                ["例如：重试 10 个 RSS 失败条目。"],
            )

        return None

    def _handle_automation_and_missing_resource_requests(
        self,
        message: str,
        *,
        lower: str,
        owner: str,
        conversation_context: list[dict[str, Any]] | None,
        query_tool_rate_identity: str,
    ) -> dict[str, Any] | None:
        """处理 STRM、光鸭整理与媒体库缺集资源请求。"""
        retry_request = strm_failure_retry_request(lower)
        if retry_request is not None:
            if not owner:
                return self._unsupported(
                    "该动作需要在已登录会话中确认",
                    ["请通过 Agent 页面重新提交，并在只读预检后确认执行。"],
                )
            return self.prepare("strm.retry_failures", retry_request, owner=owner)
        if is_strm_failure_write_message(lower):
            return self._unsupported(
                "Agent 不会自动修复、清理或删除 STRM 失败记录。",
                ["可先查看 STRM 失败状态，或明确请求重试生成失败/元数据失败。"],
            )
        if _is_strm_run_action(lower):
            if not owner:
                return self._unsupported(
                    "该动作需要在已登录会话中确认",
                    ["请通过 Agent 页面重新提交，并在预检后确认执行。"],
                )
            return self.prepare("strm.run_once", {}, owner=owner)
        if is_strm_failure_triage_message(lower):
            return self._invoke_query_read("strm.triage_failures", {})
        if is_guangya_organize_stop_message(lower):
            if not owner:
                return self._unsupported(
                    "该动作需要在已登录会话中确认",
                    ["请通过 Agent 页面重新提交，并在停止影响预检后确认执行。"],
                )
            return self.prepare("guangya.organize.stop", {}, owner=owner)
        if is_guangya_organize_clean_empty_message(lower):
            if not owner:
                return self._unsupported(
                    "该动作需要在已登录会话中确认",
                    ["请通过 Agent 页面重新提交，并在清理范围预检后确认执行。"],
                )
            return self.prepare("guangya.organize.clean_empty", {}, owner=owner)
        if is_guangya_organize_preview_message(lower):
            return self._invoke_query_read("guangya.organize.preview", {})
        if is_guangya_organize_run_message(lower):
            if not owner:
                return self._unsupported(
                    "该动作需要在已登录会话中确认",
                    ["请通过 Agent 页面重新提交，并在只读预览后确认执行。"],
                )
            return self.prepare("guangya.organize.run_once", {}, owner=owner)
        if is_missing_season_resource_search_message(lower):
            arguments = _inherit_verified_media_query(
                _extract_missing_season_resource_args(message),
                conversation_context,
                tv_only=True,
            )
            if not arguments.get("query"):
                return self._unsupported(
                    "请提供需要批量搜索缺集资源的剧名",
                    ["例如：给《某剧》第 2 季所有缺集找资源。"],
                )
            return self._invoke_query_read("library.search_missing_season_resources", arguments, owner=owner)
        if is_missing_episode_resource_search_message(lower):
            arguments = _inherit_verified_media_query(
                _extract_missing_episode_resource_args(message),
                conversation_context,
                tv_only=True,
            )
            if not arguments.get("query"):
                return self._unsupported(
                    "请提供需要搜索缺集资源的剧名",
                    ["例如：给《某剧》第 2 季第 3 集找缺集资源。"],
                )
            return self._invoke_query_read("library.search_missing_episode_resources", arguments, owner=owner)
        if is_library_series_episode_count_and_audit_message(lower):
            count_clause = re.split(
                r"[，,。！？?；;\s]*(?:(?:有没有|有无|是否)\s*)?"
                r"(?:缺集|漏集|缺少(?:的)?剧集|少了哪些集|完整性)",
                message,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0]
            count_arguments = _inherit_verified_media_query(
                _extract_library_series_episode_count_args(count_clause),
                conversation_context,
                tv_only=True,
            )
            if not count_arguments.get("query"):
                return self._unsupported(
                    "请提供需要统计并检查缺集的剧名",
                    ["例如：媒体库中《师兄啊师兄》有多少集，有没有缺集。"],
                )
            audit_arguments = _extract_episode_audit_args(message)
            # 复合请求的媒体身份以本地集数解析结果为准；缺集解析只补充季号和
            # 截止日期，避免两套自然语言抽取产生不同标题。
            audit_arguments["query"] = count_arguments["query"]
            if count_arguments.get("tmdb_id"):
                audit_arguments["tmdb_id"] = count_arguments["tmdb_id"]
            if count_arguments.get("library_name"):
                audit_arguments["library_name"] = count_arguments["library_name"]
            return self._execute_read_plan(
                LLMReadPlan(steps=(
                    LLMToolSelection(
                        tool_name="library.count_series_episodes",
                        arguments=count_arguments,
                    ),
                    LLMToolSelection(
                        tool_name="library.audit_episodes",
                        arguments=audit_arguments,
                    ),
                )),
                owner=owner,
                rate_identity=query_tool_rate_identity,
            )
        return None

    def _query_raw(
        self,
        value: Any,
        *,
        owner: str = "",
        llm_rate_owner: str = "",
        query_tool_rate_identity: str = "",
        llm_tool_rate_identity: str = "",
        conversation_context: list[dict[str, Any]] | None = None,
        allow_model_routing: bool = True,
    ) -> dict[str, Any]:
        message = normalize_agent_message(value)
        lower = message.casefold()

        discovery_followup = self._handle_discovery_followup(
            message,
            owner=owner,
            query_tool_rate_identity=query_tool_rate_identity,
        )
        if discovery_followup is not None:
            return discovery_followup

        recent_download_followup = self._handle_recent_resource_download_followup(
            message, owner=owner
        )
        if recent_download_followup is not None:
            return recent_download_followup

        # 最近巡检资源接力依赖 owner-scoped 快照，必须在模型前由服务端绑定。
        if (
            is_recent_library_patrol_resource_message(lower)
            or _is_recent_library_patrol_resource_reference(lower)
        ):
            patrol_followup = self._handle_patrol_and_schedule_requests(
                message, lower=lower, owner=owner
            )
            if patrol_followup is not None:
                return patrol_followup

        action_request = is_agent_action_request(message)
        has_resource_continuation = bool(
            owner
            and self.recent_resource_store.get(owner=owner) is not None
            and recent_resource_submit_request(message, allow_implicit=True) is not None
        )
        model_routing_attempted = False
        if (
            allow_model_routing
            and not action_request
            and not has_resource_continuation
            and not _prefer_deterministic_context_route(
                message, conversation_context
            )
        ):
            model_routing_attempted = True
            model_read = self._query_with_model_tools(
                message,
                owner=owner,
                llm_rate_owner=llm_rate_owner,
                llm_tool_rate_identity=llm_tool_rate_identity,
                conversation_context=conversation_context,
                read_only=True,
            )
            if model_read is not None:
                return model_read

        history_request = agent_action_history_request(message)
        if history_request is not None:
            return self._invoke_query_read("agent.action_history", history_request, owner=owner)

        if is_missing_workflow_status_message(message):
            return self._invoke_query_read("library.missing_media_workflows", {}, owner=owner)

        patrol_and_schedule = self._handle_patrol_and_schedule_requests(
            message, lower=lower, owner=owner
        )
        if patrol_and_schedule is not None:
            return patrol_and_schedule

        download_and_subscription = (
            self._handle_download_and_media_subscription_requests(
                message, lower=lower, owner=owner
            )
        )
        if download_and_subscription is not None:
            return download_and_subscription

        rss_response = self._handle_rss_requests(
            message, lower=lower, owner=owner
        )
        if rss_response is not None:
            return rss_response

        automation_response = (
            self._handle_automation_and_missing_resource_requests(
                message,
                lower=lower,
                owner=owner,
                conversation_context=conversation_context,
                query_tool_rate_identity=query_tool_rate_identity,
            )
        )
        if automation_response is not None:
            return automation_response

        compound_plan_request = is_compound_read_request(message)
        if compound_plan_request and allow_model_routing and not model_routing_attempted:
            # 只有前置 read-only 模型层因强上下文等原因未尝试时，才在这里
            # 为复合只读请求补一次模型机会；随后仍可回退严格 JSON read plan。
            model_routing_attempted = True
            model_compound = self._query_with_model_tools(
                message,
                owner=owner,
                llm_rate_owner=llm_rate_owner,
                llm_tool_rate_identity=llm_tool_rate_identity,
                conversation_context=conversation_context,
            )
            if model_compound is not None:
                return model_compound
        if compound_plan_request:
            plan_kwargs: dict[str, Any] = {"owner": llm_rate_owner or owner}
            if conversation_context:
                plan_kwargs["conversation_context"] = conversation_context
            plan = select_read_plan(message, self.registry, **plan_kwargs)
            if plan is not None:
                return self._execute_read_plan(
                    plan,
                    owner=owner,
                    rate_identity=query_tool_rate_identity,
                )

        if any(token in lower for token in ("能做什么", "有哪些能力", "能力", "帮助", "help", "工具列表")):
            return self._invoke_query_read("agent.capabilities", {})
        if is_workspace_next_actions_message(lower):
            if query_tool_rate_identity and not allow_agent_tool(
                query_tool_rate_identity, "workspace.next_actions"
            ):
                raise AgentToolError(
                    "Agent 请求过于频繁，请稍后重试", code="rate_limited"
                )
            return self.invoke("workspace.next_actions", {}, owner=owner)
        if is_workspace_briefing_message(lower):
            return self._invoke_query_read("workspace.briefing", {}, owner=owner)
        if is_workspace_todo_message(lower):
            return self._invoke_query_read("workspace.todo", {}, owner=owner)
        if is_workspace_search_message(lower):
            query = _extract_workspace_search_query(message)
            if not query:
                return self._unsupported(
                    "请提供需要在工作区中追踪的标题",
                    ["例如：全局搜索《沙丘2》，或《某剧》现在走到哪一步。"],
                )
            arguments: dict[str, Any] = {"query": query}
            sections = _workspace_search_sections(message)
            if sections:
                arguments["sections"] = sections
            return self._invoke_query_read("workspace.search", arguments, owner=owner)
        if is_library_series_episode_count_message(lower):
            arguments = _extract_library_series_episode_count_args(message)
            if not arguments.get("query"):
                return self._unsupported(
                    "请提供需要统计本地集数的剧名",
                    ["例如：查看媒体库中《师兄啊师兄》一共有多少集。"],
                )
            return self._invoke_query_read("library.count_series_episodes", arguments, owner=owner)
        if is_library_episode_patrol_message(lower):
            arguments = _extract_library_episode_patrol_args(message)
            # “查看/检查媒体库有没有缺集”是即时只读核对：直接读取 Jellyfin / Emby
            # 的本地集库存，再以 TMDB 已播清单为基准比较。只有用户明确要求全量、
            # 后台或分批巡检时，才进入需要确认的可恢复长任务。
            if (
                not is_explicit_background_library_episode_patrol_message(message)
                or not self.registry.has("library.start_episode_audit")
            ):
                return self._invoke_query_read(
                    "library.audit_library_episodes", arguments, owner=owner
                )
            if not owner:
                return self._unsupported(
                    "后台全库检查需要在已登录会话中确认",
                    ["请通过 Agent 页面重新提交，并在预检后确认开始。"],
                )
            # 后台巡检与即时全库核对属于同一类高成本操作，共享同一限流域。
            self._reserve_query_tool_budget("library.start_episode_audit")
            return self.prepare(
                "library.start_episode_audit",
                arguments,
                owner=owner,
            )
        if is_library_update_check_message(lower):
            extracted = _extract_library_update_args(message)
            contextual_query = _is_contextual_media_query(extracted.get("query"))
            arguments = _inherit_verified_media_query(
                extracted, conversation_context
            )
            if contextual_query and arguments.get("media_type") == "auto":
                media_type = str(
                    _latest_media_context(conversation_context).get("media_type") or ""
                ).casefold()
                if media_type in {"tv", "movie"}:
                    arguments["media_type"] = media_type
            return self._invoke_query_read("library.check_updates", arguments, owner=owner)
        if is_episode_audit_message(lower):
            arguments = _inherit_verified_media_query(
                _extract_episode_audit_args(message),
                conversation_context,
                tv_only=True,
            )
            if not arguments.get("query"):
                return self._unsupported(
                    "请提供需要核对的剧名",
                    ["例如：检查《某剧》有没有缺集，或核对《某剧》TMDB 12345。"],
                )
            return self._invoke_query_read("library.audit_episodes", arguments, owner=owner)
        organize_audit = organize_audit_request(lower)
        if organize_audit is not None:
            return self._invoke_query_read(
                "organize.audit_logs",
                organize_audit,
                owner=owner,
                rate_identity=query_tool_rate_identity,
            )
        recognition_enabled_request = recognition_rule_enabled_request(lower)
        if recognition_enabled_request is not None:
            if not owner:
                return self._unsupported(
                    "识别规则启停需要在已登录会话中确认",
                    ["请通过 Agent 页面重新提交，并在预检后确认修改。"],
                )
            return self.prepare(
                "recognition.set_rule_enabled",
                recognition_enabled_request,
                owner=owner,
            )
        if is_recognition_rule_control_message(lower):
            return self._clarification_response(
                "请明确一条识别规则的类型和编号；Agent 不会猜测目标或批量修改。",
                [
                    "启用识别预处理规则 12",
                    "停用 TMDB 正则规则 3",
                    "禁用识别知识条目 8",
                ],
            )
        media_proxy_enabled_request = media_proxy_instance_enabled_request(lower)
        if media_proxy_enabled_request is not None:
            if not owner:
                return self._unsupported(
                    "媒体反代启停需要在已登录会话中确认",
                    ["请通过 Agent 页面重新提交，并在预检后确认修改。"],
                )
            return self.prepare(
                "media_proxy.set_instance_enabled",
                media_proxy_enabled_request,
                owner=owner,
            )
        if is_media_proxy_control_message(lower):
            return self._clarification_response(
                "请明确要操作第几个媒体反代实例；Agent 不会猜测目标或批量启停。",
                [
                    "查看媒体反代状态",
                    "启用媒体反代实例 1",
                    "停用媒体反代实例 1",
                ],
            )
        media_proxy_probe_request = media_proxy_test_request(lower)
        if media_proxy_probe_request is not None:
            return self._invoke_query_read("media_proxy.test_instance", media_proxy_probe_request)
        if is_media_proxy_test_request_message(lower):
            return self._clarification_response(
                "请说明要测试第几个媒体反代实例；测试只会访问该实例已保存的上游地址。",
                ["查看媒体反代状态", "测试媒体反代实例 1"],
            )
        if is_media_proxy_status_summary_message(lower):
            return self._invoke_query_read("media_proxy.status_summary", {})
        if is_telegram_test_notification_message(lower):
            if not owner:
                return self._unsupported(
                    "Telegram 测试通知需要在已登录会话中确认",
                    ["请通过 Agent 页面重新提交，并在预检后确认发送。"],
                )
            return self.prepare(
                "telegram.send_test_notification",
                {},
                owner=owner,
            )
        local_media_request = self._handle_local_media_requests(
            lower,
            owner=owner,
            query_tool_rate_identity=query_tool_rate_identity,
        )
        if local_media_request is not None:
            return local_media_request
        diagnostic_tool = match_read_intent(lower, _DIAGNOSTIC_READ_INTENTS)
        if diagnostic_tool is not None:
            return self._invoke_query_read(diagnostic_tool, {})
        organize_scope = any(token in lower for token in ("云盘整理", "光鸭整理", "整理任务"))
        status_intent = any(token in lower for token in ("状态", "进度", "运行", "到哪", "完成", "怎么样"))
        if organize_scope and status_intent:
            return self._invoke_query_read("guangya.organize.status", {})

        # “STRM 元数据同步”是功能开关，不应被泛 STRM 状态/诊断路由截获。
        strm_metadata_request = feature_state_request(lower)
        if (
            strm_metadata_request is not None
            and strm_metadata_request.get("feature") == "strm_metadata"
        ):
            if not owner:
                return self._unsupported(
                    "该配置动作需要在已登录会话中确认",
                    ["请通过 Agent 页面重新提交，并在预检后确认修改。"],
                )
            return self.prepare(
                "config.set_feature_state", strm_metadata_request, owner=owner
            )
        if is_feature_summary_message(lower) and any(
            alias in lower
            for feature, aliases in _FEATURE_ALIASES
            if feature == "strm_metadata"
            for alias in aliases
        ):
            return self._invoke_query_read("config.feature_summary", {})
        if "strm" in lower and status_intent:
            return self._invoke_query_read("strm.status", {})
        component_request = config_component_explain_request(lower)
        if component_request is not None:
            return self._invoke_query_read("config.explain_component", component_request)
        if "strm" in lower or any(token in lower for token in ("播放链接", "索引诊断", "索引健康", "检查索引")):
            return self._invoke_query_read("strm.diagnose", {})
        if is_media_server_diagnosis_message(lower):
            return self._invoke_query_read("config.diagnose_media_servers", {})
        server_type = media_server_test_type(lower)
        if server_type:
            return self._invoke_query_read("config.test_media_server", {"server_type": server_type})
        indexer_compound_request = indexer_site_change_followup_request(
            message, conversation_context
        )
        if indexer_compound_request is not None:
            try:
                target_site_ids = resolve_indexer_site_change(
                    indexer_compound_request["change"]
                )
            except AgentToolError as exc:
                if exc.code != "precondition_failed":
                    raise
                return self._clarification_response(
                    str(exc),
                    ["查看当前启用的资源站点", "启用所有普通资源站点"],
                )
            if not owner:
                return self._unsupported(
                    "该配置动作需要在已登录会话中确认",
                    ["请通过 Agent 页面重新提交，并在预检后确认修改。"],
                )
            return self.prepare(
                "config.set_indexer_sites",
                {"site_ids": target_site_ids, "enable_search": True},
                owner=owner,
                followup_context={
                    "kind": "indexer_search_after_config",
                    "title": indexer_compound_request["title"],
                    "limit": 20,
                },
            )

        indexer_site_request = indexer_sites_request(lower)
        if indexer_site_request is not None:
            if not owner:
                return self._unsupported(
                    "该配置动作需要在已登录会话中确认",
                    ["请通过 Agent 页面重新提交，并在预检后确认修改。"],
                )
            return self.prepare(
                "config.set_indexer_sites",
                {**indexer_site_request, "enable_search": True},
                owner=owner,
            )
        indexer_site_change = indexer_site_change_request(lower)
        if indexer_site_change is not None:
            if indexer_site_change.get("operation") == "clarify_scope_conflict":
                return self._clarification_response(
                    "“普通资源站点”和“包括 Sukebei”是两个不同范围，请明确选择一个。",
                    [
                        "启用所有普通资源站点（不含 Sukebei）",
                        "启用所有资源站点，包括 Sukebei",
                    ],
                )
            try:
                target_site_ids = resolve_indexer_site_change(indexer_site_change)
            except AgentToolError as exc:
                if exc.code != "precondition_failed":
                    raise
                return self._clarification_response(
                    str(exc),
                    [
                        "查看当前启用的资源站点",
                        "启用所有普通资源站点（不含 Sukebei）",
                    ],
                )
            if not target_site_ids:
                return self._clarification_response(
                    "资源站点至少需要保留一个；如果你想停用资源检索，请直接关闭多站资源索引。",
                    [
                        "关闭多站资源索引",
                        "查看当前启用的资源站点",
                    ],
                )
            if not owner:
                return self._unsupported(
                    "该配置动作需要在已登录会话中确认",
                    ["请通过 Agent 页面重新提交，并在预检后确认修改。"],
                )
            return self.prepare(
                "config.set_indexer_sites",
                {"site_ids": target_site_ids, "enable_search": True},
                owner=owner,
            )
        if _is_vague_multi_site_request(lower):
            return self._clarification_response(
                "可以配置多个资源站点，但“所有站点”是否包含成人站点 Sukebei 并不明确。请选一个安全范围。",
                [
                    "查看当前启用的资源站点",
                    "启用所有普通资源站点（不含 Sukebei）",
                    "启用所有资源站点，包括 Sukebei",
                ],
            )
        feature_request = feature_state_request(lower)
        if feature_request is not None:
            if not owner:
                return self._unsupported(
                    "该配置动作需要在已登录会话中确认",
                    ["请通过 Agent 页面重新提交，并在预检后确认修改。"],
                )
            return self.prepare("config.set_feature_state", feature_request, owner=owner)
        safe_policy_update = safe_policy_request(lower)
        if safe_policy_update is not None:
            if not owner:
                return self._unsupported(
                    "该配置动作需要在已登录会话中确认",
                    ["请通过 Agent 页面重新提交，并在预检后确认修改。"],
                )
            return self.prepare(
                "config.set_safe_policy",
                safe_policy_update,
                owner=owner,
            )
        if is_indexer_sites_summary_message(lower):
            return self._invoke_query_read("config.indexer_sites_summary", {})
        if is_feature_summary_message(lower):
            return self._invoke_query_read("config.feature_summary", {})
        if is_safe_policy_summary_message(lower):
            return self._invoke_query_read("config.safe_policy_summary", {})
        if is_safe_policy_mutation_candidate(lower):
            return self._clarification_response(
                "一次只能修改一项受控策略，并明确给出合法值。",
                [
                    "查看当前安全策略",
                    "把 TMDB 匹配模式改为严格",
                    "把网页搜索超时改为 15 秒",
                ],
            )
        if is_config_diagnosis_message(lower):
            return self._invoke_query_read("config.diagnose", {})

        if is_web_search_message(lower):
            query = _extract_web_search_query(message)
            if not query:
                return self._unsupported(
                    "请提供需要联网搜索的关键词",
                    ["例如：联网搜索 Jellyfin 12 API 变更。"],
                )
            if query_tool_rate_identity and not allow_agent_tool(
                query_tool_rate_identity, "web.search"
            ):
                raise AgentToolError(
                    "Agent 请求过于频繁，请稍后重试", code="rate_limited"
                )
            return self.invoke("web.search", {"query": query, "max_results": 5}, owner=owner)

        if is_indexer_resource_search_message(lower):
            inherited = _inherit_verified_media_query(
                {"query": _extract_resource_search_query(message)},
                conversation_context,
            )
            query = str(inherited.get("query") or "").strip()
            if not query:
                return self._unsupported(
                    "请提供需要搜索资源的片名",
                    ["例如：搜索《沙丘2》的资源，或找《某剧》的种子。"],
                )
            if query_tool_rate_identity and not allow_agent_tool(
                query_tool_rate_identity, "indexer.search_resources"
            ):
                raise AgentToolError(
                    "Agent 请求过于频繁，请稍后重试", code="rate_limited"
                )
            return self.invoke(
                "indexer.search_resources",
                {"title": query, "limit": 20},
                owner=owner,
            )

        if is_bangumi_calendar_message(lower):
            return self._invoke_query_read("bangumi.calendar", _bangumi_calendar_arguments(message))

        if is_contextual_discovery_recommend_message(lower):
            return self._unsupported(
                "当前推荐能力只支持读取 TMDB 或豆瓣默认榜单",
                ["可以问：推荐几部电影，或豆瓣推荐电视剧。"],
            )
        if is_discovery_recommend_message(lower):
            return self._invoke_query_read(
                "discovery.recommend", _discovery_recommend_arguments(message), owner=owner
            )

        if is_discovery_search_message(lower):
            query = _extract_discovery_search_query(message)
            if not query:
                return self._unsupported(
                    "请提供需要从外部影视源搜索的片名",
                    ["例如：在网上找《沙丘2》电影，或用 TMDB 搜《黑镜》。"],
                )
            return self._invoke_query_read(
                "discovery.search", {"query": query, "limit": 20}, owner=owner
            )

        # 所有明确的领域意图优先；仅在它们都未命中且候选仍有效时，
        # 才把“第 2 个到光鸭”解释为最近资源搜索的自然续句。
        if owner and self.recent_resource_store.get(owner=owner) is not None:
            recent_resource_request = recent_resource_submit_request(
                message, allow_implicit=True
            )
            if recent_resource_request is not None:
                return self._continue_recent_resource_submit(
                    recent_resource_request, owner=owner
                )

        # 模型不可用或未可靠完成时，对“帮我找《片名》”这类明确、带引号的
        # 本地库查询保留零模型回退。
        explicit_library_query = _extract_search_query(message)
        if explicit_library_query and re.search(
            r"[《「『\"']([^》」』\"']{1,120})[》」』\"']", message
        ):
            return self._invoke_query_read(
                "library.search",
                {"query": explicit_library_query, "limit": 8},
                owner=owner,
            )

        # 动作请求或强上下文请求不会进入前置 read-only 模型层；仅在确定性
        # 业务路由也未命中时使用兼容选择器。READ 可执行，写工具只生成确认票据。
        model_routed = None
        if allow_model_routing and not model_routing_attempted:
            model_routed = self._query_with_model_tools(
                message,
                owner=owner,
                llm_rate_owner=llm_rate_owner,
                llm_tool_rate_identity=llm_tool_rate_identity,
                conversation_context=conversation_context,
                read_only=not action_request,
            )
        if model_routed is not None:
            return model_routed

        # Provider 不可用时仍保留本地媒体库标题搜索作为最后的零模型兜底。
        query = _extract_search_query(message)
        if query:
            return self._invoke_query_read(
                "library.search", {"query": query, "limit": 8}, owner=owner
            )

        conversation = answer_conversation(
            message,
            owner=llm_rate_owner or owner,
            conversation_context=conversation_context,
        )
        if conversation is not None:
            response = {
                "request_id": secrets.token_urlsafe(12),
                "mode": "conversation",
                "tool_call": None,
                "result": ToolResult(
                    ok=True,
                    status="answered",
                    summary=conversation.answer,
                    suggestions=list(conversation.suggestions),
                ).to_dict(),
            }
            guidance = result_projection.project_public_guidance(
                conversation.suggestions,
                force_kind="draft",
            )
            if guidance:
                response["guidance"] = guidance
            return result_projection.attach_public_display(response)

        return self._clarification_response(
            "我还没理解你想检查哪一项。直接说目标即可，不需要记工具名称。",
            [
                "检查下载队列有没有异常",
                "查看媒体库有没有缺集",
                "搜索《片名》的资源",
            ],
        )

    @staticmethod
    def _local_conversation(message: str) -> dict[str, Any] | None:
        if not _is_casual_greeting(message):
            return None
        response = {
            "request_id": secrets.token_urlsafe(12),
            "mode": "conversation",
            "tool_call": None,
            "result": ToolResult(
                ok=True,
                status="answered",
                summary=(
                    "我在。直接告诉我想检查什么就行，例如下载队列、媒体库缺集、"
                    "项目配置或某部影片的资源。"
                ),
            ).to_dict(),
        }
        return result_projection.attach_public_display(response)

    @staticmethod
    def _conversation_response(
        summary: str, suggestions: list[str] | tuple[str, ...] = ()
    ) -> dict[str, Any]:
        safe_summary = result_projection.sanitize_public_multiline_text(summary, limit=1800)
        safe_suggestions = [
            text
            for raw in list(suggestions)[:3]
            if (text := result_projection.sanitize_public_text(raw, limit=180))
        ]
        response = {
            "request_id": secrets.token_urlsafe(12),
            "mode": "conversation",
            "tool_call": None,
            "result": ToolResult(
                ok=True,
                status="answered",
                summary=safe_summary,
                suggestions=safe_suggestions,
            ).to_dict(),
        }
        guidance = result_projection.project_public_guidance(safe_suggestions, force_kind="draft")
        if guidance:
            response["guidance"] = guidance
        return result_projection.attach_public_display(response)

    def _continue_narrow_followup(
        self,
        message: str,
        *,
        owner: str,
        conversation_context: list[dict[str, Any]] | None,
        reply_context: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """在最近结果唯一时直接续答；多区域结果仍交给澄清层。"""
        intent = _followup_intent(message)
        if intent is None:
            return None
        previous = _last_assistant_context(conversation_context)
        previous_tool = str(previous.get("tool_name") or "").strip()
        if not previous_tool or previous_tool in _BROAD_FOLLOWUP_TOOLS:
            return None

        previous_summary = result_projection.sanitize_public_multiline_text(
            previous.get("text"), limit=800
        )
        if not previous_summary:
            previous_summary = result_projection.sanitize_public_multiline_text(
                (reply_context or {}).get("text"), limit=800
            )
        if not previous_summary:
            return None

        conversation = answer_conversation(
            message,
            owner=owner,
            conversation_context=conversation_context,
        )
        if conversation is not None:
            return self._conversation_response(
                conversation.answer, conversation.suggestions
            )

        label = result_projection.public_tool_label(previous_tool)
        suggestions = [
            text
            for raw in list(previous.get("suggestions") or [])[:3]
            if (text := result_projection.sanitize_public_text(raw, limit=180))
        ]
        if not suggestions:
            suggestions = [f"重新检查{label}"]

        if intent == "reason":
            summary = (
                f"刚才“{label}”的结论是：{previous_summary} "
                "当前保存的是安全摘要，没有足够细节时我不会猜测具体原因；"
                "可以重新检查后再直接说明异常来源。"
            )
        elif intent == "action":
            summary = (
                f"针对刚才“{label}”的结果，建议先执行：{suggestions[0]}。"
                "这一步只会生成新的检查或操作预览，不会绕过确认直接写入。"
            )
        else:
            summary = (
                f"可以继续。刚才“{label}”的结论是：{previous_summary} "
                f"下一步建议：{suggestions[0]}。"
            )
        return self._conversation_response(summary, suggestions)

    @staticmethod
    def _clarification_response(
        summary: str, suggestions: list[str]
    ) -> dict[str, Any]:
        safe_suggestions = list(suggestions[:3])
        response = {
            "request_id": secrets.token_urlsafe(12),
            "mode": "clarification",
            "tool_call": None,
            "result": ToolResult(
                ok=True,
                status="clarification_required",
                summary=summary,
                suggestions=safe_suggestions,
            ).to_dict(),
        }
        guidance = result_projection.project_public_guidance(safe_suggestions)
        if guidance:
            response["guidance"] = guidance
        return result_projection.attach_public_display(response)

    @staticmethod
    def _clarify_ambiguous_followup(
        message: str,
        *,
        conversation_context: list[dict[str, Any]] | None,
        reply_context: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not _is_ambiguous_followup(message):
            return None
        previous = _last_assistant_context(conversation_context)
        previous_tool = str(previous.get("tool_name") or "").strip()
        reply_text = result_projection.sanitize_public_text(
            (reply_context or {}).get("text"), limit=500
        )
        previous_summary = result_projection.sanitize_public_text(previous.get("text"), limit=500)
        context_available = bool(previous_tool or reply_text or previous_summary)

        if previous_tool and previous_tool not in _BROAD_FOLLOWUP_TOOLS:
            label = result_projection.public_tool_label(previous_tool)
            summary = f"你是在追问“{label}”的结果吗？请告诉我想继续看原因、影响，还是重新检查。"
            suggestions = [
                f"重新检查{label}",
                f"解释一下{label}发现的问题",
            ]
        elif context_available:
            summary = "我能继续查，但刚才的关注项来自多个区域。请先选一个方向，我会直接说明具体问题和影响。"
            suggestions = [
                "检查下载队列里的异常",
                "查看缺集巡检需要关注的内容",
                "检查 RSS 与自动化状态",
            ]
        else:
            summary = "请告诉我想关注哪一部分，我会直接检查并用普通话说明结果。"
            suggestions = [
                "检查下载队列状态",
                "查看媒体库有没有缺集",
                "检查项目配置是否正常",
            ]
        return AgentOrchestrator._clarification_response(summary, suggestions)

    @staticmethod
    def _present_tool_response(
        message: str, response: dict[str, Any], *, owner: str
    ) -> dict[str, Any]:
        if not isinstance(response, dict):
            return response
        # 原生多工具 Agent 已经产生了经过清洗的 narrative 时，不再调用第二次
        # 模型重写。这样既避免重复延迟/额度，也防止二次改写丢失候选与上下文。
        if isinstance(response.get("presentation"), dict):
            return response
        mode = str(response.get("mode") or "")
        result = response.get("result")
        tool_call = response.get("tool_call")
        if (
            mode in {"conversation", "clarification", "confirmation_required", "confirmed_action"}
            or not isinstance(result, dict)
            or not isinstance(tool_call, dict)
            or str(result.get("status") or "") in {
                "clarification_required", "selection_required", "unsupported",
                "confirmation_required",
            }
        ):
            return response
        narrative = compose_tool_answer(message, response, owner=owner)
        if narrative is None:
            return response
        presented = dict(response)
        presentation = result_projection.build_public_narrative_presentation(
            narrative.answer,
            narrative.suggestions,
        )
        if not presentation:
            return response
        presented["presentation"] = presentation
        return presented

    @staticmethod
    def _response(
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
        elapsed_ms: int,
        *,
        mode: str = "read_only",
    ) -> dict[str, Any]:
        response = {
            "request_id": secrets.token_urlsafe(12),
            "mode": mode,
            "tool_call": {"name": tool_name, "arguments": arguments, "elapsed_ms": elapsed_ms},
            "result": result.to_dict(),
        }
        guidance = result_projection.project_public_guidance(result.suggestions)
        if guidance:
            response["guidance"] = guidance
        return result_projection.attach_public_display(response)

    @staticmethod
    def _unsupported(summary: str, suggestions: list[str]) -> dict[str, Any]:
        result = ToolResult(
            ok=False,
            status="unsupported",
            summary=summary,
            suggestions=suggestions,
            error="",
        )
        response = {
            "request_id": secrets.token_urlsafe(12),
            "mode": "read_only",
            "tool_call": None,
            "result": result.to_dict(),
        }
        guidance = result_projection.project_public_guidance(suggestions)
        if guidance:
            response["guidance"] = guidance
        return result_projection.attach_public_display(response)
