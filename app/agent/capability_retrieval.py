"""Media Agent 的领域能力召回策略。

本模块只决定本轮向模型暴露哪些已注册能力，不执行工具，也不参与写权限判断。
关键词规则仅用于建立媒体领域和数据源覆盖；最终工具参数仍由模型生成并经注册表校验。
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Any, Iterable, Mapping


@dataclass(frozen=True, slots=True)
class MediaIntentProfile:
    """当前回合的有限领域画像，不包含用户原文或可执行参数。"""

    domains: tuple[str, ...] = ()
    preferred_sources: tuple[str, ...] = ()
    forbidden_sources: tuple[str, ...] = ()
    presentation_hint: str = "narrative"


_RELEASE_STATUS_RE = re.compile(
    r"(?:上线|开播|首播|播出|定档|能看|正片).{0,10}(?:了吗|没有|没|吗|时间|日期|状态)|"
    r"(?:是否|有没有|有无|都|已经|现在|目前).{0,12}(?:上线|开播|首播|播出|定档|能看|正片)",
    re.IGNORECASE,
)
_NON_MEDIA_RELEASE_RE = re.compile(
    r"(?:服务|网站|实例|容器|应用|项目|接口|端口|镜像|版本|bot|agent|api)"
    r".{0,16}(?:上线|发布|部署|启动)",
    re.IGNORECASE,
)
_OFFICIAL_PROGRESS_RE = re.compile(
    r"(?:官方|优酷|腾讯视频|爱奇艺|哔哩哔哩|bilibili|播出|正片).{0,24}"
    r"(?:更新|更到|更新至|播到|播至|第几集|多少集|多集|哪里)|"
    r"(?:更新|更到|更新至|播到|播至).{0,20}(?:第几集|多少集|多集|哪里)",
    re.IGNORECASE,
)
_EPISODE_NUMBERING_RE = re.compile(
    r"(?:第\s*[0-9零〇一二两三四五六七八九十百千]+\s*季\s*第?\s*"
    r"[0-9零〇一二两三四五六七八九十百千]+\s*集|"
    r"(?<![A-Za-z0-9])S0*\d{1,3}\s*E0*\d{1,4})"
    r".{0,24}(?:总第几集|累计第几集|是多少集|算第几集|对应第几集)",
    re.IGNORECASE,
)
_RESOURCE_MARKERS = (
    "资源", "种子", "磁力", "torrent", "magnet", "nyaa", "下载源", "资源站",
)
_LIBRARY_MARKERS = (
    "媒体库", "本地库", "jellyfin", "emby", "入库", "本地收录", "缺集", "漏集",
    "本地有", "本地多少", "本地几集",
)
_DOMAIN_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("system", ("系统状态", "运行状态", "系统正常", "项目正常", "系统健康", "系统简报")),
    ("jobs", ("正在运行", "运行中的任务", "后台任务", "任务进度", "任务状态")),
    ("agent", ("agent", "智能助手", "能力列表", "操作历史")),
    ("subscriptions", ("追更", "订阅", "rss", "mikan")),
    ("downloads", ("下载队列", "qbittorrent", "qb", "下载任务", "离线任务", "离线下载")),
    ("indexer", ("索引站", "资源站", "资源搜索", "搜索资源", "搜不到资源")),
    ("local_media", ("本地媒体来源", "本地整理", "本地下载目录", "本机整理")),
    ("organize", ("整理", "归档", "刮削", "光鸭整理")),
    ("strm", ("strm",)),
    ("library", ("媒体库", "jellyfin", "emby", "缺几集", "有多少集", "入库")),
    ("rating", ("评分", "豆瓣分", "tmdb分", "bangumi分")),
    ("discovery", ("推荐", "找电影", "找剧", "搜电影", "搜剧", "影视资料", "片荒")),
    ("playback", ("播放失败", "无法播放", "媒体反代", "反代实例", "302", "转码")),
    ("recognition", ("识别规则", "识别错误", "tmdb识别", "错季")),
    ("config", ("配置", "开关", "设置", "启用", "禁用")),
)


def _normalize(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


def _has_negated_scope(text: str, markers: Iterable[str]) -> bool:
    alternatives = "|".join(re.escape(marker) for marker in markers)
    return bool(re.search(
        rf"(?:不要|不用|别|无需|不必|不查|不看|不搜).{{0,10}}(?:{alternatives})",
        text,
        re.IGNORECASE,
    ))


def infer_media_intent(value: object) -> MediaIntentProfile:
    """从有限媒体语义生成能力召回画像；不用于决定是否执行写操作。"""
    text = _normalize(value)
    if not text:
        return MediaIntentProfile()

    domains: list[str] = []
    preferred_sources: list[str] = []
    forbidden_sources: list[str] = []

    release_status = bool(
        _RELEASE_STATUS_RE.search(text)
        and not _NON_MEDIA_RELEASE_RE.search(text)
    )
    official_progress = release_status or bool(_OFFICIAL_PROGRESS_RE.search(text)) or (
        "官方" in text
        and any(marker in text for marker in ("播", "进度", "更新", "第几集", "多少集"))
    )
    episode_numbering = bool(_EPISODE_NUMBERING_RE.search(text))
    resource_negated = _has_negated_scope(text, _RESOURCE_MARKERS)
    library_negated = _has_negated_scope(text, _LIBRARY_MARKERS)
    resource_search = (
        any(marker in text for marker in _RESOURCE_MARKERS) and not resource_negated
    )
    local_library = (
        any(marker in text for marker in _LIBRARY_MARKERS) and not library_negated
    )

    if official_progress:
        domains.append("official_progress")
        # 官方上线/播出事实默认只需要公开时效数据。只有用户明确询问
        # 本地收录或资源跟进时，才把对应数据源加入本轮。
        preferred_sources.append("public_web")
        if local_library:
            preferred_sources.append("local_library")
        if resource_search:
            preferred_sources.append("resource_index")
    if episode_numbering:
        domains.append("episode_numbering")
        preferred_sources.extend(("public_web", "local_library"))
    if resource_search:
        domains.append("resource_search")
        preferred_sources.append("resource_index")
    if local_library:
        domains.append("library")
        preferred_sources.append("local_library")

    for domain, markers in _DOMAIN_MARKERS:
        if any(marker in text for marker in markers):
            domains.append(domain)

    # “光鸭”同时承载云端整理与离线下载。只有出现明确流程词时才绑定领域，
    # 避免“查看光鸭离线任务”被整理工具淹没，也避免“整理光鸭目录”只召回下载。
    if "光鸭" in text:
        if any(marker in text for marker in ("离线", "下载", "推送", "磁力", "种子")):
            domains.append("downloads")
        if any(marker in text for marker in ("整理", "归档", "刮削", "目录", "清理", "改名")):
            domains.append("organize")

    if "只查官方" in text or "只看官方" in text or "只要官方" in text:
        forbidden_sources.extend(("local_library", "resource_index"))
    elif official_progress:
        if not local_library:
            forbidden_sources.append("local_library")
        if not resource_search:
            forbidden_sources.append("resource_index")
    if "只查本地" in text or "只看本地" in text or "只查媒体库" in text:
        forbidden_sources.extend(("public_web", "resource_index"))
    if resource_negated:
        forbidden_sources.append("resource_index")
    if library_negated:
        forbidden_sources.append("local_library")
    if _has_negated_scope(text, ("网页", "联网", "官方平台")):
        forbidden_sources.append("public_web")

    forbidden = tuple(dict.fromkeys(forbidden_sources))
    preferred = tuple(
        source for source in dict.fromkeys(preferred_sources)
        if source not in forbidden
    )
    explicit_resource_command = bool(re.search(
        r"(?:搜索|搜一下|查找|找一下|找找|找资源|补资源|补集|下载|提交|推送|选择)"
        r".{0,16}(?:资源|种子|磁力|torrent|magnet)|"
        r"(?:资源|种子|磁力|torrent|magnet).{0,12}(?:搜索|查找|下载|提交|推送)",
        text,
        re.IGNORECASE,
    ))
    informational_resource_check = bool(re.search(
        r"(?:有没有|有无|是否有|跟上|跟进|到哪|只告诉|只看|只查).{0,12}"
        r"(?:资源|种子|磁力)|(?:资源|种子|磁力).{0,12}(?:有吗|跟上|跟进|到哪|最新)",
        text,
        re.IGNORECASE,
    ))
    presentation = (
        "resource_candidates"
        if resource_search
        and explicit_resource_command
        and not informational_resource_check
        and not official_progress
        else "narrative"
    )
    return MediaIntentProfile(
        domains=tuple(dict.fromkeys(domains)),
        preferred_sources=preferred,
        forbidden_sources=forbidden,
        presentation_hint=presentation,
    )


def capability_semantics(capability: Mapping[str, Any]) -> dict[str, Any]:
    """读取显式 ToolSpec 语义，并为旧插件提供集中式保守默认值。"""
    raw = capability.get("semantics")
    raw = raw if isinstance(raw, Mapping) else {}
    name = str(capability.get("name") or "").strip()
    domains = tuple(
        str(item).strip().lower()
        for item in raw.get("domains", ())
        if str(item).strip()
    )
    source_kind = str(raw.get("source_kind") or "").strip().lower()
    evidence_role = str(raw.get("evidence_role") or "primary").strip().lower()
    freshness = str(raw.get("freshness") or "snapshot").strip().lower()
    parallel_safe = raw.get("parallel_safe") is not False
    workflow = str(raw.get("workflow") or "").strip().lower()
    try:
        workflow_stage = int(raw.get("workflow_stage") or 0)
    except (TypeError, ValueError):
        workflow_stage = 0
    confirmation_followup = str(raw.get("confirmation_followup") or "").strip()
    inferred_from_name = not domains

    if not domains:
        if name == "web.search":
            domains = ("official_progress", "research")
        elif name == "indexer.search_resources":
            domains = ("resource_search", "official_progress")
        elif name == "library.check_updates":
            domains = ("library", "official_progress")
        elif name == "library.count_series_episodes":
            domains = ("library", "episode_numbering")
        elif name.startswith("library."):
            domains = ("library",)
        elif name.startswith("workspace."):
            domains = ("system", "jobs")
        elif name.startswith("automation."):
            domains = ("system", "downloads", "subscriptions", "organize", "strm")
        elif name.startswith("local_media."):
            domains = ("local_media", "organize", "library")
        elif name.startswith("agent."):
            domains = ("agent", "jobs")
        elif name.startswith("rss.") or name.startswith("media.subscription"):
            domains = ("subscriptions",)
        elif name.startswith("media.continue_watching"):
            domains = ("library", "playback")
        elif name.startswith("media.preference") or name.startswith("media.set_preference"):
            domains = ("config", "downloads", "library")
        elif name.startswith("media.today"):
            domains = ("system", "downloads", "subscriptions", "organize")
        elif name.startswith("downloads."):
            domains = ("downloads",)
        elif name == "guangya.connection_status":
            domains = ("downloads", "organize", "config")
        elif name.startswith("guangya.") or name.startswith("organize."):
            domains = ("organize",)
        elif name.startswith("strm."):
            domains = ("strm",)
        elif name.startswith("discovery."):
            domains = ("discovery",)
        elif name.startswith("indexer."):
            domains = ("indexer", "resource_search")
        elif name.startswith("media_proxy."):
            domains = ("playback",)
        elif name.startswith("recognition."):
            domains = ("recognition",)
        elif name.startswith("config."):
            domains = ("config",)

    if not source_kind or (inferred_from_name and source_kind == "system_state"):
        if name == "web.search":
            source_kind = "public_web"
        elif name.startswith("indexer.") or name.endswith("_resources"):
            source_kind = "resource_index"
        elif name.startswith("library."):
            source_kind = "local_library"
        elif name.startswith("discovery."):
            source_kind = "metadata_catalog"
        else:
            source_kind = "system_state"

    return {
        "domains": domains,
        "source_kind": source_kind,
        "evidence_role": evidence_role,
        "freshness": freshness,
        "parallel_safe": parallel_safe,
        "workflow": workflow,
        "workflow_stage": workflow_stage,
        "confirmation_followup": confirmation_followup,
    }


def capability_intent_boost(
    capability: Mapping[str, Any], intent: MediaIntentProfile
) -> float:
    semantics = capability_semantics(capability)
    source_kind = semantics["source_kind"]
    if source_kind in intent.forbidden_sources:
        return -1000.0
    domains = set(semantics["domains"])
    score = 0.0
    overlap = domains.intersection(intent.domains)
    if overlap:
        score += 24.0 + (6.0 * (len(overlap) - 1))
    if source_kind in intent.preferred_sources:
        score += 8.0
    if (
        intent.domains
        and semantics["evidence_role"] == "supporting"
        and intent.presentation_hint == "narrative"
    ):
        score += 1.0
    return score


def ensure_source_coverage(
    selected: Iterable[dict[str, Any]],
    eligible: Iterable[dict[str, Any]],
    intent: MediaIntentProfile,
    *,
    max_candidates: int,
) -> list[dict[str, Any]]:
    """在候选预算内补齐官方/本地/资源来源，保持单 Agent 可一次完成核验。"""
    chosen = [
        item for item in selected
        if capability_semantics(item)["source_kind"] not in intent.forbidden_sources
    ]
    chosen_names = {str(item.get("name") or "").strip() for item in chosen}
    available = [
        item for item in eligible
        if capability_semantics(item)["source_kind"] not in intent.forbidden_sources
    ]

    def best_for_source(source_kind: str) -> dict[str, Any] | None:
        candidates = [
            item for item in available
            if capability_semantics(item)["source_kind"] == source_kind
            and str(item.get("name") or "").strip() not in chosen_names
        ]
        if not candidates:
            return None
        domain_set = set(intent.domains)
        candidates.sort(key=lambda item: (
            -len(set(capability_semantics(item)["domains"]).intersection(domain_set)),
            str(item.get("name") or ""),
        ))
        return candidates[0]

    for source_kind in intent.preferred_sources:
        if source_kind in intent.forbidden_sources:
            continue
        if any(
            capability_semantics(item)["source_kind"] == source_kind
            for item in chosen
        ):
            continue
        candidate = best_for_source(source_kind)
        if candidate is None:
            continue
        if len(chosen) >= max_candidates:
            # 来源覆盖优先替换没有命中当前领域且不是其他必需来源的尾部能力。
            replaced = False
            for index in range(len(chosen) - 1, -1, -1):
                semantics = capability_semantics(chosen[index])
                if (
                    not set(semantics["domains"]).intersection(intent.domains)
                    and semantics["source_kind"] not in intent.preferred_sources
                ):
                    chosen_names.discard(str(chosen[index].get("name") or "").strip())
                    chosen[index] = candidate
                    replaced = True
                    break
            if not replaced:
                continue
        else:
            chosen.append(candidate)
        chosen_names.add(str(candidate.get("name") or "").strip())

    return chosen[:max_candidates]


def capability_prompt_hint(capability: Mapping[str, Any]) -> str:
    """把工具数据源边界压缩为提供给模型的短说明。"""
    semantics = capability_semantics(capability)
    source_labels = {
        "public_web": "公开网页；官方平台信息优先，可用于时效性事实",
        "local_library": "本地媒体库；只证明本地收录或本地状态",
        "resource_index": "资源索引；只能证明资源跟进，不能证明官方播出",
        "metadata_catalog": "影视元数据目录；不能替代官方实时公告",
        "system_state": "MediaFlux 当前系统状态",
    }
    source = source_labels.get(semantics["source_kind"], semantics["source_kind"])
    role = "辅助旁证" if semantics["evidence_role"] == "supporting" else "主要证据"
    freshness = {
        "realtime": "实时读取",
        "live": "当前读取",
        "cached": "可能来自缓存",
        "snapshot": "状态快照",
        "derived": "派生事实",
        "historical": "历史记录",
    }.get(semantics["freshness"], semantics["freshness"])
    return f"数据边界：{source}；{role}；{freshness}。"
