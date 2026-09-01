"""不可执行的结构化媒体事实；与资源候选和确认状态严格分离。"""
from __future__ import annotations

import re
import unicodedata
from copy import deepcopy
from datetime import date
from typing import Any, Iterable

from app.sensitive_data import contains_sensitive_credential

_MEDIA_FACTS_VERSION = 1
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SOURCE_KINDS = frozenset({"web", "library", "indexer", "tmdb", "conversation"})
_OFFICIAL_WEB_HOST_LABELS: tuple[tuple[str, str], ...] = (
    ("youku.com", "优酷官方"),
    ("v.qq.com", "腾讯视频官方"),
    ("iqiyi.com", "爱奇艺官方"),
    ("bilibili.com", "哔哩哔哩官方"),
)
_HUMAN_NUMBER_RE = r"(?:[0-9]{1,4}|[零〇一二两三四五六七八九十百千]{1,7})"


def _safe_text(value: Any, maximum: int) -> str:
    text = " ".join(unicodedata.normalize("NFKC", str(value or "")).split()).strip()
    if not text or len(text) > maximum or _CONTROL_RE.search(text):
        return ""
    if contains_sensitive_credential(text):
        return ""
    return text


def _positive_int(value: Any, maximum: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        return None
    return value


def _human_number(value: str) -> int | None:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    if normalized.isascii() and normalized.isdigit():
        parsed = int(normalized)
        return parsed if 0 <= parsed <= 9999 else None
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
              "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    units = {"十": 10, "百": 100, "千": 1000}
    total = 0
    current = 0
    for char in normalized:
        if char in digits:
            current = digits[char]
        elif char in units:
            total += (current or 1) * units[char]
            current = 0
        else:
            return None
    parsed = total + current
    return parsed if 0 <= parsed <= 9999 else None


def _normalized_identity(value: Any) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKC", str(value or "")).casefold()
        if char.isalnum()
    )


def _safe_identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    title = _safe_text(value.get("title"), 160)
    if not title:
        return {}
    result: dict[str, Any] = {"title": title}
    original_title = _safe_text(value.get("original_title"), 160)
    if original_title:
        result["original_title"] = original_title
    year = str(value.get("year") or "").strip()
    if re.fullmatch(r"(?:18|19|20|21|22)\d{2}", year):
        result["year"] = year
    media_type = str(value.get("media_type") or "").strip().lower()
    if media_type in {"movie", "tv", "anime"}:
        result["media_type"] = media_type
    for key, maximum_digits in (("tmdb_id", 10), ("bangumi_id", 10), ("douban_id", 20)):
        identifier = str(value.get(key) or "").strip()
        if identifier.isascii() and identifier.isdigit() and 1 <= len(identifier) <= maximum_digits:
            result[key] = identifier
    return result


def _safe_progress(value: Any, *, allow_count: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key, maximum in (("season", 100), ("episode", 1000), ("absolute_episode", 5000)):
        parsed = _positive_int(value.get(key), maximum)
        if parsed is not None:
            result[key] = parsed
    if allow_count:
        count = _positive_int(value.get("count"), 10000)
        if count is not None:
            result["count"] = count
    as_of = str(value.get("as_of") or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of):
        result["as_of"] = as_of
    source = str(value.get("source") or "").strip().lower()
    if source in _SOURCE_KINDS:
        result["source"] = source
    return result


def validate_media_facts(value: Any) -> dict[str, Any]:
    """验证并返回可持久化、不可执行的媒体事实投影。"""
    if not isinstance(value, dict):
        return {}
    version = value.get("version", _MEDIA_FACTS_VERSION)
    if version != _MEDIA_FACTS_VERSION:
        return {}
    identity = _safe_identity(value.get("identity"))
    if not identity:
        return {}
    result: dict[str, Any] = {"version": _MEDIA_FACTS_VERSION, "identity": identity}
    for key, allow_count in (
        ("official_progress", False),
        ("local_progress", True),
        ("indexer_progress", False),
    ):
        progress = _safe_progress(value.get(key), allow_count=allow_count)
        if progress:
            result[key] = progress
    raw_counts = value.get("season_counts")
    if isinstance(raw_counts, list):
        counts: list[dict[str, Any]] = []
        seen: set[int] = set()
        for item in raw_counts[:100]:
            if not isinstance(item, dict):
                continue
            season = _positive_int(item.get("season"), 100)
            episodes = _positive_int(item.get("episodes"), 1000)
            source = str(item.get("source") or "").strip().lower()
            if season is None or episodes is None or season in seen:
                continue
            record: dict[str, Any] = {"season": season, "episodes": episodes}
            if source in _SOURCE_KINDS:
                record["source"] = source
            counts.append(record)
            seen.add(season)
        if counts:
            result["season_counts"] = sorted(counts, key=lambda item: item["season"])
    return result


def _latest_context_facts(context: Iterable[dict[str, Any]] | None) -> dict[str, Any]:
    for item in reversed(list(context or [])):
        if not isinstance(item, dict):
            continue
        facts = validate_media_facts(item.get("media_facts"))
        if facts:
            return facts
    return {}


def _identity_from_context(context: Iterable[dict[str, Any]] | None) -> dict[str, Any]:
    for item in reversed(list(context or [])):
        if not isinstance(item, dict):
            continue
        media_context = item.get("media_context")
        if isinstance(media_context, dict):
            identity = _safe_identity(media_context)
            if identity:
                return identity
    return {}


def _identity_from_executions(executions: Iterable[dict[str, Any]]) -> dict[str, Any]:
    for execution in reversed(list(executions)):
        if not isinstance(execution, dict):
            continue
        tool_name = str(execution.get("tool_name") or "").strip()
        arguments = execution.get("arguments") if isinstance(execution.get("arguments"), dict) else {}
        response = execution.get("response") if isinstance(execution.get("response"), dict) else {}
        result = response.get("result") if isinstance(response.get("result"), dict) else {}
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        # web.search 的 query 通常包含“官方、更新至、平台名”等检索修饰词，
        # 不能把整条搜索语句持久化成媒体标题；此时交给当前消息或已有上下文提取身份。
        query_title = (
            data.get("query") or arguments.get("query")
            if tool_name != "web.search" else ""
        )
        identity = _safe_identity({
            "title": data.get("title") or arguments.get("title") or query_title,
            "original_title": data.get("original_title") or arguments.get("original_title"),
            "year": data.get("year") or arguments.get("year"),
            "media_type": data.get("media_type") or arguments.get("media_type"),
            "tmdb_id": data.get("tmdb_id") or arguments.get("tmdb_id"),
        })
        if identity:
            return identity
    return {}


def _identity_from_message(message: str) -> dict[str, Any]:
    normalized = _safe_text(message, 1000)
    quoted = re.search(r"[《「『\"']([^》」』\"']{1,160})[》」』\"']", normalized)
    if quoted:
        return {"title": quoted.group(1).strip()}
    title = re.sub(
        r"(?i)(?:只查官方|只看官方|只要官方|请帮我|帮我|请|核对一下|查一下|官方|现在|目前|动画|动漫|电视剧|剧集|正片|全系列|本地|资源|索引|"
        r"更新到(?:第)?几集|更新到多集|更新到哪里|更新至(?:第)?几集|更新至哪里|播到(?:第)?几集|播到哪里|最新(?:到|是)?(?:第)?几集|"
        r"第\s*\d+\s*季第\s*\d+\s*集(?:是|算|对应)?(?:总)?第几集|"
        r"总第几集|是多少集|啦|了|吗|呢|[?？])",
        " ", normalized,
    )
    title = " ".join(title.split()).strip(" ，。！？?、:：")
    return {"title": title[:160]} if 1 <= len(title) <= 160 else {}


def _execution_data(execution: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    tool_name = str(execution.get("tool_name") or "").strip()
    arguments = execution.get("arguments") if isinstance(execution.get("arguments"), dict) else {}
    response = execution.get("response") if isinstance(execution.get("response"), dict) else {}
    result = response.get("result") if isinstance(response.get("result"), dict) else {}
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    return tool_name, arguments, data


def _season_counts_from_answer(answer: str, *, source: str) -> list[dict[str, Any]]:
    counts: dict[int, int] = {}
    patterns = (
        rf"第\s*({_HUMAN_NUMBER_RE})\s*季[^。；;\n]{{0,24}}?(?:共|总共|合计|全)\s*([0-9]{{1,4}})\s*集",
        r"(?<![A-Za-z0-9])S\s*0*([0-9]{1,3})[^。；;\n]{0,20}?(?:共|总共|合计|全)\s*([0-9]{1,4})\s*集",
    )
    for pattern in patterns:
        for raw_season, raw_count in re.findall(pattern, answer, flags=re.IGNORECASE):
            season = _human_number(raw_season)
            episodes = int(raw_count)
            if season is not None and 1 <= season <= 100 and 1 <= episodes <= 1000:
                counts[season] = episodes
    return [
        {"season": season, "episodes": episodes, "source": source}
        for season, episodes in sorted(counts.items())
    ]


def _answer_date(answer: str) -> str:
    iso = re.search(r"(?<!\d)((?:19|20|21|22)\d{2})[-/.]([01]?\d)[-/.]([0-3]?\d)(?!\d)", answer)
    if iso:
        year, month, day = map(int, iso.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            pass
    chinese = re.search(r"((?:19|20|21|22)\d{2})\s*年\s*([01]?\d)\s*月\s*([0-3]?\d)\s*日", answer)
    if chinese:
        year, month, day = map(int, chinese.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            pass
    return ""


def _official_progress_from_answer(answer: str) -> dict[str, Any]:
    if not re.search(r"官方|优酷|腾讯视频|爱奇艺|哔哩哔哩|bilibili|正片", answer, re.IGNORECASE):
        return {}
    pattern = re.compile(
        rf"第\s*({_HUMAN_NUMBER_RE})\s*季[^。；;\n]{{0,100}}?"
        rf"(?:更新至|更新到|当前至|目前至|播到|已播至|正片已更新至)\s*第?\s*({_HUMAN_NUMBER_RE})\s*集",
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(answer))
    if not matches:
        return {}
    matched = matches[-1]
    season = _human_number(matched.group(1))
    episode = _human_number(matched.group(2))
    if season is None or episode is None or not 1 <= season <= 100 or not 1 <= episode <= 1000:
        return {}
    result: dict[str, Any] = {"season": season, "episode": episode, "source": "web"}
    absolute = re.search(r"(?:累计|全系列|总)(?:到|至|为)?\s*第?\s*([0-9]{1,4})\s*集", answer)
    if absolute:
        parsed = int(absolute.group(1))
        if 1 <= parsed <= 5000:
            result["absolute_episode"] = parsed
    as_of = _answer_date(answer)
    if as_of:
        result["as_of"] = as_of
    return result


def _official_web_label(source: Any) -> str:
    host = str(source or "").strip().casefold().rstrip(".")
    for suffix, label in _OFFICIAL_WEB_HOST_LABELS:
        if host == suffix or host.endswith("." + suffix):
            return label
    return ""


def _official_web_evidence_texts(data: dict[str, Any]) -> list[str]:
    """投影官方站点的可核验标题/摘要；不读取 LLM 最终叙述。"""
    results = data.get("results")
    if not isinstance(results, list):
        return []
    texts: list[str] = []
    for item in results[:10]:
        if not isinstance(item, dict):
            continue
        title = _safe_text(item.get("title"), 240)
        snippet = _safe_text(item.get("snippet"), 900)
        if not title and not snippet:
            continue
        official_label = _official_web_label(item.get("source"))
        if not official_label:
            continue
        parts = [
            value for value in (
                official_label,
                title,
                snippet,
                _safe_text(item.get("published_date"), 40),
            )
            if value
        ]
        projected = _safe_text(" ".join(parts), 1400)
        if projected:
            texts.append(projected)
    return texts


def _resource_coordinates(title: Any) -> tuple[int, int] | None:
    text = unicodedata.normalize("NFKC", str(title or ""))
    explicit = re.search(r"(?i)(?<![A-Za-z0-9])S\s*0*(\d{1,3})[ ._\-]*E(?:P)?\s*0*(\d{1,4})(?!\d)", text)
    if explicit:
        season, episode = map(int, explicit.groups())
        return (season, episode) if 1 <= season <= 100 and 1 <= episode <= 1000 else None
    season_match = re.search(r"第\s*0*(\d{1,3})\s*季", text)
    if not season_match:
        return None
    season = int(season_match.group(1))
    episode_match = re.search(r"(?:第\s*)?0*(\d{1,4})\s*(?:集|话)", text)
    if not episode_match:
        brackets = [
            int(raw) for raw in re.findall(r"[\[【(（]\s*0*(\d{1,4})\s*[\]】)）]", text)
            if 1 <= int(raw) <= 1000 and int(raw) not in {360, 480, 540, 576, 720}
        ]
        episode = brackets[-1] if brackets else 0
    else:
        episode = int(episode_match.group(1))
    return (season, episode) if 1 <= season <= 100 and 1 <= episode <= 1000 else None


def _tool_facts(executions: Iterable[dict[str, Any]]) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    official_candidates: list[dict[str, Any]] = []
    web_season_counts: dict[int, set[int]] = {}
    indexer_best: tuple[int, int] | None = None
    for execution in executions:
        if not isinstance(execution, dict):
            continue
        tool_name, _arguments, data = _execution_data(execution)
        if tool_name == "web.search":
            for evidence_text in _official_web_evidence_texts(data):
                official = _official_progress_from_answer(evidence_text)
                if official:
                    official_candidates.append(official)
                for item in _season_counts_from_answer(
                    evidence_text, source="web"
                ):
                    web_season_counts.setdefault(item["season"], set()).add(
                        item["episodes"]
                    )
        if tool_name in {"library.count_series_episodes", "library.audit_episodes", "library.check_updates"}:
            local: dict[str, Any] = {"source": "library"}
            count = data.get("local_episode_count")
            if isinstance(count, int) and not isinstance(count, bool) and 1 <= count <= 10000:
                local["count"] = count
            seasons = data.get("seasons")
            if isinstance(seasons, list):
                valid = [item for item in seasons if isinstance(item, dict)]
                if valid:
                    latest = max(valid, key=lambda item: int(item.get("season") or 0))
                    season = latest.get("season")
                    episode = latest.get("last_episode")
                    if isinstance(season, int) and isinstance(episode, int):
                        local.update({"season": season, "episode": episode})
            if len(local) > 1:
                facts["local_progress"] = local
        if tool_name in {"indexer.search_resources", "library.search_missing_episode_resources", "library.search_missing_season_resources"}:
            for item in data.get("items") or data.get("candidates") or []:
                if not isinstance(item, dict):
                    continue
                coordinates = _resource_coordinates(item.get("title"))
                if coordinates and (indexer_best is None or coordinates > indexer_best):
                    indexer_best = coordinates
    if official_candidates:
        facts["official_progress"] = max(
            official_candidates,
            key=lambda item: (
                int(item.get("season") or 0),
                int(item.get("episode") or 0),
                str(item.get("as_of") or ""),
            ),
        )
    counts = [
        {"season": season, "episodes": next(iter(values)), "source": "web"}
        for season, values in sorted(web_season_counts.items())
        if len(values) == 1
    ]
    if counts:
        facts["season_counts"] = counts
    if indexer_best is not None:
        facts["indexer_progress"] = {
            "season": indexer_best[0], "episode": indexer_best[1], "source": "indexer"
        }
    return facts


def _identities_match(left: Any, right: Any) -> bool:
    left_identity = _safe_identity(left)
    right_identity = _safe_identity(right)
    if not left_identity or not right_identity:
        return False

    compared_identifier = False
    for key in ("tmdb_id", "bangumi_id", "douban_id"):
        left_value = str(left_identity.get(key) or "").strip()
        right_value = str(right_identity.get(key) or "").strip()
        if not left_value or not right_value:
            continue
        compared_identifier = True
        if left_value != right_value:
            return False
    if compared_identifier:
        return True

    left_title = _normalized_identity(left_identity.get("title"))
    right_title = _normalized_identity(right_identity.get("title"))
    if not left_title or left_title != right_title:
        return False
    left_year = str(left_identity.get("year") or "").strip()
    right_year = str(right_identity.get("year") or "").strip()
    if left_year and right_year and left_year != right_year:
        return False
    left_type = str(left_identity.get("media_type") or "").strip().lower()
    right_type = str(right_identity.get("media_type") or "").strip().lower()
    if left_type == "anime":
        left_type = "tv"
    if right_type == "anime":
        right_type = "tv"
    return not (left_type and right_type and left_type != right_type)


def _merge_same_identity(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    previous = validate_media_facts(previous)
    current = validate_media_facts(current)
    if not current:
        return previous
    if not previous:
        return current
    if not _identities_match(previous["identity"], current["identity"]):
        return current
    merged = deepcopy(previous)
    merged["identity"].update(current["identity"])
    for key in ("official_progress", "local_progress", "indexer_progress"):
        if key in current:
            merged[key] = current[key]
    counts = {item["season"]: dict(item) for item in merged.get("season_counts", [])}
    for item in current.get("season_counts", []):
        counts[item["season"]] = dict(item)
    if counts:
        merged["season_counts"] = [counts[key] for key in sorted(counts)]
    return validate_media_facts(merged)


def derive_media_facts(
    *,
    message: str,
    answer: str,
    executions: Iterable[dict[str, Any]],
    conversation_context: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """从已执行的受控只读工具提取可跨轮使用的有限事实。"""
    execution_list = [item for item in executions if isinstance(item, dict)]
    identity = (
        _identity_from_executions(execution_list)
        or _identity_from_context(conversation_context)
        or _identity_from_message(message)
    )
    identity = _safe_identity(identity)
    if not identity:
        return {}
    current = {"version": _MEDIA_FACTS_VERSION, "identity": identity}
    # 最终叙述由模型生成，不作为可持久化事实来源；只从受控工具结果提取。
    current.update(_tool_facts(execution_list))
    current = validate_media_facts(current)
    previous = _latest_context_facts(conversation_context)
    return _merge_same_identity(previous, current)


def media_facts_match_context(facts: Any, media_context: Any) -> bool:
    """判断结构化事实是否属于同一媒体身份，避免压缩时交叉串台。"""
    validated = validate_media_facts(facts)
    context_identity = _safe_identity(media_context)
    if not validated or not context_identity:
        return False
    return _identities_match(validated["identity"], context_identity)


def media_facts_for_llm(value: Any) -> dict[str, Any]:
    """返回可直接进入 Provider 上下文的安全副本。"""
    return deepcopy(validate_media_facts(value))


def latest_media_facts(context: Iterable[dict[str, Any]] | None) -> dict[str, Any]:
    """公开的安全上下文读取入口。"""
    return deepcopy(_latest_context_facts(context))


def absolute_episode_answer(
    message: str,
    conversation_context: Iterable[dict[str, Any]] | None,
) -> str:
    """Provider 不可用时，仅按已存结构化季长做季度到总集数换算。"""
    normalized = unicodedata.normalize("NFKC", str(message or ""))
    matched = re.search(
        rf"第?\s*({_HUMAN_NUMBER_RE})\s*季\s*(?:第\s*)?"
        rf"({_HUMAN_NUMBER_RE})\s*集[^。！？!?]{{0,20}}?"
        r"(?:是|算|对应|等于|为)?\s*(?:总|累计|全剧)?\s*第?\s*(?:几|多少)\s*集",
        normalized,
        re.IGNORECASE,
    ) or re.search(
        r"(?i)(?<![A-Za-z0-9])S0*(\d{1,3})\s*E0*(\d{1,4})"
        r"[^。！？!?]{0,20}?(?:总|累计|全剧)?\s*第?\s*(?:几|多少)\s*集",
        normalized,
    )
    if not matched:
        return ""
    season = _human_number(matched.group(1))
    episode = _human_number(matched.group(2))
    if season is None or episode is None or not 1 <= season <= 100 or not 1 <= episode <= 1000:
        return ""
    facts = _latest_context_facts(conversation_context)
    if not facts:
        return ""
    official = facts.get("official_progress")
    if (
        isinstance(official, dict)
        and official.get("season") == season
        and official.get("episode") == episode
        and isinstance(official.get("absolute_episode"), int)
    ):
        total = int(official["absolute_episode"])
        return (
            f"第 {season} 季第 {episode} 集是全剧累计第 {total} 集。\n\n"
            f"季度编号是 S{season:02d}E{episode:02d}，连续总集数编号是第 {total} 集。"
        )
    counts = {
        int(item["season"]): int(item["episodes"])
        for item in facts.get("season_counts", [])
        if isinstance(item, dict)
        and isinstance(item.get("season"), int)
        and isinstance(item.get("episodes"), int)
    }
    required = list(range(1, season))
    if any(number not in counts for number in required):
        return ""
    previous_total = sum(counts[number] for number in required)
    total = previous_total + episode
    title = _safe_text((facts.get("identity") or {}).get("title"), 160)
    prefix = f"《{title}》" if title else ""
    equation = " + ".join([*(str(counts[number]) for number in required), str(episode)])
    return (
        f"{prefix}第 {season} 季第 {episode} 集是全剧累计第 {total} 集。\n\n"
        f"换算：{equation} = {total}。标准季度编号是 S{season:02d}E{episode:02d}。"
    )
