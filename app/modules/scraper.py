"""TMDB 刮削模块。

解决"a 认成 aa"的核心：
1. guessit 解析文件名 → 标题/年份/类型/季集
2. 干扰词清洗（剥离字幕组/画质/编码/分辨率/来源）
3. TMDB 搜索 + 严格匹配（标题相似度 + 年份加权）
4. 低于阈值进"待确认"，Top3 候选供预览
5. 映射锁缓存（已确认的 raw_name→tmdb_id 不再重复猜）
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from datetime import date
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Optional

from app.clients.openai_compatible import (
    normalize_provider_location,
    resolve_protocol,
)
from app.clients.ai_recognition import (
    AIRecognitionClient,
    AIRecognitionError,
    AIRecognitionProviderError,
    AIRecognitionUnavailableError,
    AIRecognitionInput,
    AIRecognitionResult,
    AIReleaseGroupInput,
    AIReleaseGroupResult,
)
from app.clients.tmdb import TMDBClient, close_tmdb_client
from app.config import get, get_bool
from app.discovery.models import ProviderRateLimited, ProviderTimeout, ProviderUnavailable
from app.logger import get_logger, redact_sensitive_text
from app.sensitive_data import contains_sensitive_credential
from app.modules.ai_recognition_governance import provider_fingerprint
from app.modules.recognition.models import (
    ReleaseParseEvidence,
    ReleaseParseToken,
)
from app.modules.recognition.cleaner import (
    _comparison_key,
    _split_title_variants,
    _unique_text,
    strip_media_file_suffix,
)
from app.modules.recognition.extractors.deterministic import (
    _extract_episode,
    _extract_explicit_season,
    _extract_special_episode,
    _has_unaccepted_release_x_position,
    _parse_release_x_position,
    _valid_unlabeled_episode,
)
from app.modules.recognition.extractors.guessit_adapter import (
    _guessit_cached,
    _guessit_episode_is_untrusted,
    _guessit_info,
    _position_number,
    _season_number,
)
from app.repositories.recognition import get_tmdb_lock, upsert_tmdb_lock
from app.modules.recognition.resolver import (
    _explicit_tmdb_id_from_path,
    _has_explicit_tmdb_marker,
    _resolve_explicit_tmdb_marker,
    _source_year_matches_tmdb,
    _strict_non_negative_int,
    _strip_explicit_tmdb_markers,
    _strip_explicit_tmdb_markers_from_path,
    _tmdb_position_error,
    _validate_tmdb_position,
)
from app.modules.recognition_policy import (
    automatic_match_policy,
    normalize_automatic_match_preset,
)
from app.modules.special_media import (
    is_special_directory_name,
    special_media_position,
    strip_special_media_markers,
)
from app.modules.episode_mapping import (
    EpisodeMappingPlan,
    infer_episode_mapping,
    infer_merged_season_cour_mapping,
    season_episode_counts,
)

logger = get_logger(__name__)

MAX_ALIAS_VALIDATION_CANDIDATES = 8
# 拉丁/Romaji 查询必须先覆盖完整的有界搜索候选，才能证明“同名候选只有
# 这些”。这个上限只控制详情补全覆盖面；真正进入位置消歧的精确同名候选
# 仍受 MAX_ALIAS_VALIDATION_CANDIDATES=8 的更严格保护。
MAX_ALIAS_ENRICHMENT_CANDIDATES = 20
_SEARCH_CACHE_LIMIT = 256
_SEARCH_CACHE_TTL_SECONDS = 300.0
_EMPTY_SEARCH_CACHE_TTL_SECONDS = 30.0
_TMDB_FAILURE_CACHE_TTL_SECONDS = 15.0
_TMDB_FAILURE_CACHE_LIMIT = 256
_TMDB_CIRCUIT_THRESHOLD = 3
_TMDB_CIRCUIT_COOLDOWN_SECONDS = 60.0
_AI_RESULT_CACHE_TTL_SECONDS = 300.0
_AI_FAILURE_CACHE_TTL_SECONDS = 5.0
_AI_FAILURE_CACHE_LIMIT = 128
_RECOGNITION_DETAIL_CACHE_LIMIT = 128
_AI_FAILURE_GENERIC = "generic"
_AI_FAILURE_PROVIDER = "provider"
_AI_FAILURE_UNAVAILABLE = "unavailable"


def _ai_failure_kind(exc: BaseException) -> str:
    if isinstance(exc, AIRecognitionUnavailableError):
        return _AI_FAILURE_UNAVAILABLE
    if isinstance(exc, AIRecognitionProviderError):
        return _AI_FAILURE_PROVIDER
    return _AI_FAILURE_GENERIC


def _ai_failure_error_type(kind: str) -> type[AIRecognitionError]:
    if kind == _AI_FAILURE_UNAVAILABLE:
        return AIRecognitionUnavailableError
    if kind == _AI_FAILURE_PROVIDER:
        return AIRecognitionProviderError
    return AIRecognitionError

AIClientKey = (
    tuple[str, str, str, int, str]
    | tuple[str, str, str, int, str, str]
)

# 干扰词：字幕组 / 画质 / 编码 / 分辨率 / 来源 / 季集标记 / 年份

_CR_WEBRIP_SOURCE = re.compile(
    r"(?i)(?<![a-z0-9])(?:cr|c[ ._-]+r)[ ._-]+(?=web-?rip\b)"
)

_NOISE = re.compile(
    r'(?i)\b('
    r'web-?dl|webrip|bluray|blu-?ray|remux|bdrip|brrip|dvdrip|vhsrip|hdtv|pdtv|cam|ts|tc|'
    r'hd[ ._-]?(?:2160|1080|720)p|2160p|1080p|720p|480p|576p|4k|uhd|'
    r'hq(?=[ ._-]+(?:web-?dl|webrip|2160p|1080p|hdr|h[ ._-]?26[45]))|'
    r'hdr|hdr10|hdr10plus|dolby|atmos|truehd|'
    r'(?:ddp|eac3|aac|dts|flac|opus)[ ._-]?(?:1|2|3|5|7)[ ._-]?[01]|'
    r'ddp|ddp5|ddp7|eac3|ac3|aac|dts|dts-hd|dts-ma|flac|mp3|opus|(?:2|3|5|7)[ ._-]?1|'
    r'h[ ._-]?264|h[ ._-]?265|x264|x265|hevc|avc|vp9|av1|10bit|10-bit|8bit|'
    r'23\.976fps|24fps|25fps|30fps|60fps|fps|'
    r'netflix|nf|amazon|amzn|disney|dsnp|hbo[ ._-]?max|hbo|hulu|atvp|'
    r'apple[ ._-]?tv\+?|apple|itunes|catchplay|bilibili|baha|'
    r'cht|chs|big5|gb|mp4|mkv|assx?\d*|srtx?\d*|'
    r'colortv|color?tv|dreamhd|ddhdtv|bitsrc|frds|'
    r'complete|全集|全季|全\s*\d+\s*集|全[零〇一二两三四五六七八九十]{1,3}集|finale|'
    r's\d{1,2}e\d{1,4}(?:v\d+)?|s\d{1,2}|e\d{1,4}(?:v\d+)?|'
    r'season\s?\d+|episode\s?\d+|第\d+季|'
    r'第\d+(?:集|[话話])(?=$|[\s._\-—–:：,，;；\[\]【】()（）])|EP?\d+'
    r')\b'
)

# 语言轨标识只在独立括号标签中视为噪声，避免误删合法标题
# （例如 ``Japanese Story``）。
_BRACKET_SHORT_LANGUAGE_NOISE = re.compile(r"(?i)jpn")
_BRACKET_LONG_LANGUAGE_NOISE = re.compile(r"(?i)japanese")


@dataclass(frozen=True)
class _SearchOutcome:
    results: tuple[dict, ...] = ()
    status: str = "no_result"
    error: str = ""
    cache_hit: bool = False
    empty_cache_hit: bool = False


@dataclass
class RecognitionContext:
    """从文件名和父目录提取出的、可序列化的确定性识别上下文。"""

    filename: str
    parent_path: str = ""
    normalized_title: str = ""
    filename_title: str = ""
    filename_year: str = ""
    folder_title: str = ""
    folder_year: str = ""
    media_type: str = "movie"
    season: int | None = None
    episode: int | None = None
    title_variants: list[str] = field(default_factory=list)
    cleaned_components: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class ReleaseParseResult:
    """文件名、父目录和预处理规则融合后的统一解析结果。

    所有识别消费者统一使用本对象，避免同一文件在不同阶段得到不同季集位置。
    """

    filename: str
    parent_path: str
    title: str
    year: str
    media_type: str
    tmdb_id: str
    source_season: int | None
    source_episode: int | None
    effective_season: int | None
    effective_episode: int | None
    context: RecognitionContext
    preprocess_rules: tuple[dict[str, object], ...] = ()
    tokens: tuple[ReleaseParseToken, ...] = ()
    evidence: tuple[ReleaseParseEvidence, ...] = ()

    def diagnostic_dict(self) -> dict[str, object]:
        return {
            "filename": self.filename,
            "parent_path": self.parent_path,
            "title": self.title,
            "year": self.year,
            "media_type": self.media_type,
            "tmdb_id": self.tmdb_id,
            "source_position": {
                "season": self.source_season, "episode": self.source_episode,
            },
            "effective_position": {
                "season": self.effective_season, "episode": self.effective_episode,
            },
            "title_variants": list(self.context.title_variants),
            "cleaned_components": {
                key: list(values) for key, values in self.context.cleaned_components.items()
            },
            "preprocess_rules": [dict(item) for item in self.preprocess_rules],
            "tokens": [item.to_dict() for item in self.tokens],
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass
class CandidateScoreBreakdown:
    """候选评分的结构化数值解释，便于 UI 和测试稳定消费。"""

    title_score: float = 0.0
    original_title_score: float = 0.0
    alias_score: float = 0.0
    year_score: float = 0.0
    year_penalty: float = 0.0
    media_type_score: float = 0.0
    constraint_penalty: float = 0.0
    quarter_bonus: float = 0.0
    final_score: float = 0.0
    matched_title: str = ""
    matched_query: str = ""
    rejected_constraints: list[str] = field(default_factory=list)


@dataclass
class Candidate:
    tmdb_id: str
    title: str
    year: str
    score: float
    media_type: str = ""
    original_title: str = ""
    overview: str = ""
    poster_path: str = ""
    backdrop_path: str = ""
    release_date: str = ""
    aliases: list[str] = field(default_factory=list)
    score_breakdown: CandidateScoreBreakdown | None = None
    provider: str = "tmdb"
    external_id: str = ""
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class MatchResult:
    tmdb_id: str = ""
    title: str = ""
    year: str = ""
    media_type: str = ""  # movie / tv
    confidence: float = 0.0
    candidates: list[Candidate] = field(default_factory=list)
    locked: bool = False       # 来自映射锁
    need_confirm: bool = False  # 低于阈值，需人工确认
    error: str = ""
    status: str = ""
    matched_by: str = ""
    threshold: float = 0.0
    regex_rule_id: int | None = None
    season_override: int | None = None
    preprocess_evaluated: bool = False
    recognition_filename: str = ""
    recognition_parent_path: str = ""
    effective_season: int | None = None
    effective_episode: int | None = None
    preprocess_rules: list[dict[str, object]] = field(default_factory=list)
    provider: str = ""
    external_id: str = ""
    metadata: dict[str, object] = field(default_factory=dict)
    # 仅原生、严格通过的 TMDB 搜索结果可提升为目录级身份缓存。
    # BGM/豆瓣、AI、Tavily、人工规则等结果即使最终通过，也只能绑定当前
    # 文件/任务，避免辅助线索被无意放大到同目录的其它媒体。
    directory_identity_cache_eligible: bool = False


@dataclass
class RecognitionResult(MatchResult):
    context: RecognitionContext | None = None
    query_variants: list[str] = field(default_factory=list)
    threshold_decision: dict[str, object] = field(default_factory=dict)
    rejected_constraints: list[str] = field(default_factory=list)
    ai_diagnostic: dict[str, object] = field(default_factory=dict)


_RELEASE_PREFIX = re.compile(r"^\s*(\[[^\]]{1,96}\]|【[^】]{1,96}】)\s*")
_SITE_PREFIX = re.compile(
    # 必须是由点分隔的完整主机名，且最后一个 label 才是受支持 TLD。
    # 旧表达式会把 ``LIAR.GAME.`` 回溯解释为以 ``me`` 结尾的网站前缀，
    # 从而把真实标题整体删除。这里仍兼容 ``www.Site.com@Title`` 与
    # ``foo.me_Title``，但不再在普通单词内部匹配伪 TLD。
    r"^\s*@?((?:[A-Za-z0-9-]+\.)+(?:site|tv|com|net|org|cc|me|cn))"
    r"(?:@|[._ -]+)",
    re.IGNORECASE,
)
_CHECKSUM_SUFFIX = re.compile(
    r"(?:[\[【](?P<bracket>[A-Fa-f0-9]{8,40})[\]】]|"
    r"\((?P<parenthesized>[A-Fa-f0-9]{8,40})\))\s*$"
)
_TMDB_ANIMATION_GENRE_ID = 16
_EXPLICIT_DONGHUA_MARKER = re.compile(r"(?i)(?<![a-z0-9])donghua(?![a-z0-9])")
_YEAR_TOKEN = re.compile(
    r"(?<![\dxX])((?:19|20)\d{2})(?!\d|[xX]\d{3,4})"
)
_ORDINAL_SEASON_TOKEN = re.compile(
    r"(?i)(?<!\d)(\d{1,2})(?:st|nd|rd|th)[ ._-]*season\b"
)
_ENGLISH_ORDINAL_SEASON_TOKEN = re.compile(
    r"(?i)\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"eleventh|twelfth|thirteenth|fourteenth|fifteenth|sixteenth|seventeenth|"
    r"eighteenth|nineteenth|twentieth)[ ._-]+season\b"
)
_ENGLISH_ORDINAL_SEASONS = {
    word: index
    for index, word in enumerate((
        "first", "second", "third", "fourth", "fifth", "sixth", "seventh",
        "eighth", "ninth", "tenth", "eleventh", "twelfth", "thirteenth",
        "fourteenth", "fifteenth", "sixteenth", "seventeenth", "eighteenth",
        "nineteenth", "twentieth",
    ), start=1)
}
# 某些动画续作把季号写进官方副标题（例如 ``2nd Attack``）。该标记只用于
# 季号解析，不参与标题清洗，避免把官方标题退化成“不要欺负我，长瀞同学”。
_ORDINAL_ATTACK_SEASON_TOKEN = re.compile(
    r"(?i)(?<!\d)(\d{1,2})(?:st|nd|rd|th)[ ._-]+attack"
    r"(?=\s*(?:[（(【\[].{0,48}[）)】\]])?\s*(?:-|e(?:p(?:isode)?)?|第|[【\[]|$))"
)
_IMPLICIT_SEASON_EPISODE_TOKEN = re.compile(
    r"(?ix)(?<![A-Za-z0-9])"
    r"(?P<season>20|1[0-9]|[2-9]|VIII|VII|VI|IV|III|II|IX|V|X|"
    r"Ⅷ|Ⅶ|Ⅵ|Ⅳ|Ⅲ|Ⅱ|Ⅸ|Ⅴ|Ⅹ)"
    r"(?![A-Za-z0-9])"
    r"(?=\s*(?:[-_.]+\s*)+(?:e(?:p(?:isode)?)?[ ._-]*)?"
    r"\d{1,4}(?:v\d+)?(?=\s*(?:[\[【(（]|$)))"
)
# 动画压制组也常把续季与单集都写成方括号：``Title II [05] [1080p]``。
# 该形式不复用普通 ``Title II - 05`` 的表达式，便于在证据校验阶段施加更
# 严格的“后续仍有技术标签”要求，避免把普通作品名末尾的罗马数字误当季号。
_IMPLICIT_SEASON_BRACKET_EPISODE_TOKEN = re.compile(
    r"(?ix)(?<![A-Za-z0-9])"
    r"(?P<season>20|1[0-9]|[2-9]|VIII|VII|VI|IV|III|II|IX|V|X|"
    r"Ⅷ|Ⅶ|Ⅵ|Ⅳ|Ⅲ|Ⅱ|Ⅸ|Ⅴ|Ⅹ)"
    r"(?![A-Za-z0-9])"
    r"(?=\s*[\[【(（]\s*\d{1,4}(?:v\d+)?\s*[\]】)）]"
    r"\s*[\[【(（])"
)
# 部分动画续季会使用 Unicode 兼容罗马数字，并在季号后附带由全角横线
# 包裹的正式副标题，例如 ``Clevatess Ⅱ－副标题－ - 08 [1080P]``。
# 该结构无法命中普通的 ``Title II - 08`` 规则；这里要求完整的 CJK
# 副标题、明确单集号和发布技术证据，避免把 ``Lupin Ⅲ - 08`` 一类作品
# 身份中的罗马数字直接解释为季号。
_IMPLICIT_CJK_SUBTITLE_SEASON_EPISODE_TOKEN = re.compile(
    r"(?ix)(?<![A-Za-z0-9])"
    r"(?P<season>Ⅱ)"
    r"(?![A-Za-z0-9])"
    r"(?P<subtitle>\s*(?P<subtitle_sep>[－—–])\s*"
    r"[\u3040-\u30ff\u3400-\u9fff][^\[\]()（）\r\n]{3,118}?"
    r"(?P=subtitle_sep))"
    r"(?=\s+(?:[-_.]+\s*)+(?:e(?:p(?:isode)?)?[ ._-]*)?"
    r"\d{1,4}(?:v\d+)?(?=\s*(?:[\[【(（]|$)))"
)
# ``[Yami Shibai 17][06][1080p]`` 同时存在两种合理解释：作品名
# 可能就叫 ``Yami Shibai 17``，也可能是基础标题的第 17 季。该模式不能在
# 纯文件名解析阶段直接定性；它只生成一个严格 TMDB 回退候选，且必须由目标
# 季/集确实存在来证明后，才通过 ``season_override`` 影响最终整理位置。
_AMBIGUOUS_ENCLOSED_HIGH_SEASON_EPISODE_TOKEN = re.compile(
    r"(?ix)[\[【]"
    r"(?P<title>[^\]】\r\n]{2,120}?)"
    r"[ ._-]+(?P<season>20|1[0-9])\s*[\]】]\s*"
    r"[\[【]\s*(?P<episode>\d{1,4})(?:v\d+)?\s*[\]】]"
    r"(?=\s*[\[【])"
)
_IMPLICIT_SEASON_VOLUME_TOKEN = re.compile(
    r"(?ix)(?<![A-Za-z0-9])"
    r"(?P<season>20|1[0-9]|[2-9]|VIII|VII|VI|IV|III|II|IX|V|X|"
    r"Ⅷ|Ⅶ|Ⅵ|Ⅳ|Ⅲ|Ⅱ|Ⅸ|Ⅴ|Ⅹ)"
    r"(?![A-Za-z0-9])"
    r"(?P<volume>\s+(?:vol(?:ume)?)[ ._-]*\d{1,2})"
    r"(?=\s*(?:[\[【(（]|$))"
)
# 少数日文动画把第二季写成标题尾部的罗马字 ``Ni!!``。该词在普通
# 日语罗马字中也可能是助词，因此这里只接受“作品标题本身以感叹号结束 +
# 独立 Ni + 至少两个感叹号 + 后接明确集号”的发布结构；最终仍必须通过
# TMDB 第 2 季/集存在性校验，不能仅凭该标记自动归档。
_ROMAJI_SECOND_SEASON_EPISODE_TOKEN = re.compile(
    r"(?ix)(?<=[!！])\s+"
    r"(?P<season>Ni)(?P<marker>[!！]{2,})"
    r"(?=\s*(?:[-_.]+\s*)+(?:e(?:p(?:isode)?)?[ ._-]*)?"
    r"\d{1,4}(?:v\d+)?(?=\s*(?:[\[【(（]|$)))"
)
# 同一类续作也会直接把阿拉伯季号粘在作品结尾感叹号后，例如
# ``碧蓝航线 微速前进！2！！ - 06``。这里只接受“原作品以感叹号结束 +
# 2-9 + 至少两个感叹号 + 明确单集号”的极窄结构；普通 ``Title 2``、
# ``86 2`` 或作品名中的数字均不会命中，最终仍需 TMDB 季集存在性校验。
_PUNCTUATED_NUMERIC_SEASON_EPISODE_TOKEN = re.compile(
    r"(?ix)(?<=[!！])"
    r"(?P<season>[2-9])(?P<marker>[!！]{2,})"
    r"(?=\s*(?:[-_.]+\s*)+(?:e(?:p(?:isode)?)?[ ._-]*)?"
    r"\d{1,4}(?:v\d+)?(?=\s*(?:[\[【(（]|$)))"
)
_AMBIGUOUS_SEQUEL_PREDECESSOR = re.compile(
    r"(?i)(?:^|[\s._-])(?:part|cour|volume|vol|disc|disk|cd|chapter|"
    r"movie|film|episode|ep)\s*$"
)
_UNRESOLVED_SEQUEL_HINT = re.compile(
    r"(?i)(?:^|[\s._-])(?:part|cour|volume|vol|disc|disk|cd|chapter)"
    r"[ ._-]*(?:20|1[0-9]|[2-9]|VIII|VII|VI|IV|III|II|IX|V|X)"
    r"(?=$|[\s._\-\[【(（])"
)
_SEASON_TOKEN = re.compile(
    r"(?i)(?:\bseason[ ._-]*|\bs)(\d{1,2})(?:\b|e\d{1,4}(?:v\d+)?)"
    r"|第\s*(\d{1,2})\s*季"
    r"|(?<!\d)(\d{1,2})(?:st|nd|rd|th)[ ._-]*season\b"
)
# 少量动画发布把季号写成独立的 ``[TV-3]`` / ``[ТВ-3]``。只接受
# 完整方括号、1-99 的季号，避免把 ``TV-1080`` 或普通标题片段误判为季号。
_BRACKET_TV_SEASON_TOKEN = re.compile(
    r"(?i)[\[【]\s*(?:tv|тв)\s*-\s*(?P<season>0[1-9]|[1-9]\d?)\s*[\]】]"
)
_EPISODE_TOKEN = re.compile(
    r"(?i)\bs\d{1,2}e(\d{1,4})(?:v\d+)?"
    r"(?=$|[\s._\-—–:：,，;；\[\]【】()（）])"
    r"|(?:\be(?:p(?:isode)?)?[ ._-]*)(\d{1,4})(?:v\d+)?"
    r"(?=$|[\s._\-—–:：,，;；\[\]【】()（）])"
    r"|第\s*(\d{1,3})\s*(?:集|[话話])"
    r"(?=$|[\s._\-—–:：,，;；\[\]【】()（）])"
)
_CHINESE_EPISODE_TOKEN = re.compile(
    r"第\s*([零〇一二两三四五六七八九十]{1,3})\s*(?:集|[话話])"
    r"(?=$|[\s._\-—–:：,，;；\[\]【】()（）])"
)
_CHINESE_SEASON_TOKEN = re.compile(r"第\s*([零〇一二两三四五六七八九十]{1,3})\s*季")
_CHINESE_SEASON_COMPLETION_TOKEN = re.compile(
    r"第\s*(?:\d{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*季\s*(?:全集|全季)"
)
# 国漫发布目录中的“年番1/年番4”描述具体播出批次，不属于作品正式标题。
# 未编号的“年番”可能是稳定节目名的一部分（如“斗破苍穹 年番”），必须保留。
_CHINESE_ANNUAL_RELEASE_TOKEN = re.compile(
    r"(?<![\u3400-\u9fff])年番\s*\d{1,2}(?![\u3400-\u9fff])"
)
_SEASON_RANGE_TOKEN = re.compile(
    r"(?i)(?:\bs\d{1,2}\s*[-~～–—]\s*s\d{1,2}\b"
    r"|第\s*[零〇一二两三四五六七八九十\d]{1,3}\s*"
    r"[-~～–—]\s*[零〇一二两三四五六七八九十\d]{1,3}\s*季)"
)
_SPECIAL_EPISODE_TOKEN = re.compile(
    r"(?i)(?:^|[ ._\-\[\(【])(?:"
    r"ova(?:[ ._-]*(\d{1,3}))?"
    r"|oav(?:[ ._-]*(\d{1,3}))?"
    r"|oad(?:[ ._-]*(\d{1,3}))?"
    r"|sp(?:[ ._-]*(\d{1,3}))?"
    r"|specials?(?:[ ._-]*(\d{1,3}))?"
    r"|(?:特别篇|特別篇|番外篇?|特典)(?:[ ._-]*(\d{1,3}))?"
    r")(?=$|[ ._\-\]\)】])"
)
_SPECIAL_EPISODE_RANGE = re.compile(
    r"(?i)(?:^|[ ._\-\[\(【])(?:ova|oad|oav|sp|specials?|特别篇|特別篇|番外篇?|特典)"
    r"[ ._\-]*(\d{1,3})\s*[-~～–—]\s*(\d{1,3})"
    r"(?=$|[ ._\-\]\)】])"
)
_GENERIC_FOLDER = re.compile(
    r"(?i)^(?:movies?|films?|tv|shows?|series|电影|影片|剧集|电视剧|动漫|纪录片|综艺|"
    r"season\s*\d+|s\d+|第\d+季|disc\s*\d+|cd\s*\d+|1080p|2160p|4k|"
    r"[\[【]\s*(?:tv|тв)\s*-\s*(?:0[1-9]|[1-9]\d?)\s*[\]】])$"
)
_AVAILABILITY_TAG = re.compile(
    r"[\(（\[【]\s*(?:仅限|僅限|限)"
    r"[^()（）\[\]【】]{1,24}(?:[\)）\]】]|$)",
    re.IGNORECASE,
)
_BARE_EPISODE_SUFFIX = re.compile(
    r"(?i)(?:^|\s+-\s+|[._]+)(\d{1,4})(?:v\d+)?"
    r"(?:[._]+)?(?=\s*(?:[\[【(（]|$))"
)
_BRACKET_EPISODE_TOKEN = re.compile(r"(?i)[\[【(（]\s*(\d{1,4})(?:v\d+)?\s*[\]】)）]")
_BRACKET_EPISODE_SUFFIX = re.compile(r"(?i)[\[【(（]\s*(\d{1,4})(?:v\d+)?\s*[\]】)）]\s*$")
_BRACKET_EPISODE_RANGE = re.compile(
    r"(?i)^\s*(?:(?:e?p?\s*)?\d{1,3}\s*[-~～–—]\s*\d{1,3}"
    r"(?:\s*(?:fin(?:al)?|complete|全集))?|"
    r"第\s*\d{1,3}\s*[-~～–—]\s*\d{1,3}\s*(?:集|[话話]))\s*$"
)
_BRACKETED_SEGMENT = re.compile(r"[\[【(（]([^\]】)）]{1,160})[\]】)）]")
_RELEASE_EPISODE_RANGE = re.compile(
    r"(?i)\bs\d{1,2}e(\d{1,3})\s*[-~～–—]\s*(?:e)?(\d{1,3})\b"
    r"|(?:^|[ ._\-\[\(【])e(?:p(?:isode)?)?[ ._-]*(\d{1,3})"
    r"\s*[-~～–—]\s*(?:e(?:p(?:isode)?)?[ ._-]*)?(\d{1,3})"
    r"(?=$|[ ._\-\]\)】])"
)
_BARE_COMPLETED_EPISODE_RANGE = re.compile(
    r"(?i)(?:^|[\s._\-\[\(【])(?:e?p?\s*)?(\d{1,3})\s*[-~～–—]\s*(\d{1,3})"
    r"\s*(?:fin(?:al)?|complete|全集)(?=$|[\s._\-\]\)】])"
)
_RELEASE_X_POSITION = re.compile(
    r"(?i)(?<![A-Za-z0-9])(\d{1,2})x(\d{1,3})(?![A-Za-z0-9])"
)
_RELEASE_X_NUMERIC_TOKEN = re.compile(
    r"(?i)(?<![A-Za-z0-9])(\d{1,4})x(\d{1,4})(?![A-Za-z0-9])"
)
_RELEASE_X_ASPECT_RATIOS = frozenset({(4, 3), (16, 9), (21, 9), (32, 9)})
_RELEASE_REVISION_BRACKET = re.compile(
    r"(?i)[\[【(（]\s*(v\d{1,3})\s*[\]】)）]"
)
_RELEASE_GROUP_BRACKET = re.compile(
    r"(?i)^(?:(?:.+[-_. ])?(?:fansub|raws?|subs?)|"
    r"(?:.+)?(?:字幕组|字幕組|字幕社|制作组|製作組|发布组|發布組|發佈組|"
    r"压制组|壓制組|压制部|壓制部))$"
)
_STRUCTURED_EPISODE_POSITION = re.compile(
    r"(?i)(?<![A-Za-z0-9])s\d{1,2}e\d{1,4}(?:v\d+)?(?![A-Za-z0-9])"
)
_BRACKET_RELEASE_META_NOISE = re.compile(
    r"(?i)^(?:cr|iq|adn|repack|readnfo|rartv|multi(?:-?subs?)?|msubs?|"
    r"subfrench|vostfr|jpn?|jap|rus|tver|tv|(?:tv|тв)\s*-\s*(?:0[1-9]|[1-9]\d?)|"
    r"bd|fhd|mpeg2|vhsrip|audio|version[ ._-]*light|final)$"
)
_BRACKET_RELEASE_DATE = re.compile(
    r"^((?:19|20)\d{2})([./-])(0?[1-9]|1[0-2])\2"
    r"(0?[1-9]|[12]\d|3[01])$"
)
_UNBRACKETED_RELEASE_DATE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})([./-])(0?[1-9]|1[0-2])\2"
    r"(0?[1-9]|[12]\d|3[01])(?!\d)"
)
_EXPLICIT_SUBTITLE_MARKER = re.compile(
    r"(?i)(?:双语|雙語|多语|多語|内嵌|內嵌|内封|內封|外挂|外掛|字幕)"
)
_EXPLICIT_SUBTITLE_LANGUAGE = re.compile(
    r"(?i)(?:简体?|簡體?|繁体?|繁體?|中文?|国语?|國語?|粤语?|粵語?|"
    r"日(?:文|语|語)?|英(?:文|语|語)?|韩(?:文|语|語)?|韓(?:文|語)?|"
    r"chs|cht|jpn?|ja|sc|tc|gb|big5)"
)
_EXPLICIT_SUBTITLE_ALLOWED = re.compile(
    r"(?i)(?:简体?|簡體?|繁体?|繁體?|中文?|国语?|國語?|粤语?|粵語?|"
    r"日(?:文|语|語)?|英(?:文|语|語)?|韩(?:文|语|語)?|韓(?:文|語)?|"
    r"chs|cht|jpn?|ja|sc|tc|gb|big5|双语|雙語|多语|多語|"
    r"内嵌|內嵌|内封|內封|外挂|外掛|字幕|配音|多音轨|多音軌|"
    r"音轨|音軌|mp4|mkv|[\s+&/._,，、-])+"
)
_RELEASE_LANGUAGE_NAME_AFTER_TECH = re.compile(
    r"(?i)(?P<technical>web-?dl|webrip|bluray|blu-?ray|remux|"
    r"h[ ._-]?26[45]|x26[45]|hevc|avc|aac|ddp|eac3|ac3|dts|flac|opus)"
    r"(?P<separator>[ ._-]+)"
    r"(?P<language>english|chinese|mandarin|cantonese|japanese|korean)"
    r"(?=[ ._-]+(?:chs|cht|sc|tc|eng|jpn|ja|gb|big5)(?:[ ._-]|$))"
)
_BRACKET_DIMENSION_NOISE = re.compile(r"(?i)^\d{3,4}x\d{3,4}$")
_TRAILING_RELEASE_COMPLETION = re.compile(
    r"(?P<separator>[\s._-]+)(?P<tag>FINAL|FIN)\s*$"
)
_TRAILING_BRACKET_RELEASE_COMPLETION = re.compile(
    r"(?i)(?P<separator>[\s._-]*)(?:[\[【(（])\s*"
    r"(?P<tag>COMPLETE)\s*(?:[\]】)）])\s*$"
)
_PRIMARY_TRAILING_RELEASE_EDITION_NOISE = re.compile(
    r"(?ix)(?:[ ._\-—–:：]+|^)"
    r"(?:(?:\d{1,3}(?:st|nd|rd|th))[ ._\-]*remaster(?:ed)?|"
    r"(?:4k|uhd)[ ._\-]*remaster(?:ed)?)$"
)
_LEGACY_MULTI_TECH_TAIL = re.compile(
    r"(?ix)\bvhsrip\b"
    r"(?=.*\b(?:\d{3,4}p|x26[45]|h[ ._-]?26[45]|hevc|avc|aac|flac|dts)\b)"
    r".*?(?P<separator>[ ._-]+)(?P<tag>multi(?:-?subs?)?|msubs?)\s*$"
)
_IMPLICIT_SEASON_TECHNICAL_EVIDENCE = re.compile(
    r"(?i)(?:2160p|1080p|720p|480p|4k|uhd|web[ ._-]?(?:dl|rip)|"
    r"blu[ ._-]?ray|bdrip|remux|h[ ._-]?26[45]|x26[45]|hevc|avc|"
    r"(?:aac|ddp|eac3|dts|flac)(?:[ ._-]?[257]\.?1)?)"
)
_RELEASE_SOURCE_BRACKET = re.compile(
    r"(?i)^(?:(?:高清影视之家|高清剧集网)(?:发布)?\s*)?"
    r"(?:www\.)?[a-z0-9][a-z0-9.-]{1,62}\.(?:com|net|org|tv|cc|me|cn)$"
)
_RELEASE_KIND_VERSION_BRACKET = re.compile(
    r"(?i)^(?:(?:movie|film|ova|oad|special|sp|剧场版|劇場版|电影|電影)"
    r"(?:[ ._-]*v\d{1,3})?|v\d{1,3})$"
)
_RELEASE_LANGUAGE_BRACKET = re.compile(
    r"(?i)^(?:jpn?|ja|chs|cht|sc|tc|gb|big5|jpsc|jptc|jpchs|jpcht|"
    r"(?:jp|jpn?)[+&/._-]?(?:sc|tc|chs|cht)|"
    r"(?:sc|tc|chs|cht)[+&/._-]?(?:jp|jpn?))$"
)
_BRACKET_DUB_AUDIO_NOISE = re.compile(
    r"(?i)^(?:国语配音|國語配音|粤语配音|粵語配音|台配|港配|"
    r"(?:korean|japanese|english|chinese|mandarin|cantonese)[ ._-]+audio|"
    r"dub(?:bed)?|dual[ ._-]?audio)$"
)
_BRACKET_RELEASE_EDITION_NOISE = re.compile(
    r"(?i)^(?:\d{2,3}\s*(?:fps|帧率|幀率)(?:版本|版)?|"
    r"高(?:码|碼)(?:率)?(?:版本|版))$"
)
_BRACKET_STREAMING_PLATFORM_NOISE = re.compile(
    r"(?i)^(?:wetv|tving|iqiyi|youku|viki|viu|crunchyroll|"
    r"netflix|nf|amazon|amzn|disney|dsnp|hbo|hulu|atvp|apple|itunes|"
    r"catchplay|bilibili|baha)$"
)
# 域名后缀本身不是技术证据；只允许它作为已经具备分辨率/来源/编码证据的
# 复合括号中的附属噪声，例如 ``[1080p BILIBILI COM WEB-DL]``。这样不会
# 把 ``[BILIBILI COM]`` 或包含真实标题词的括号整体删除。
_BRACKET_DOMAIN_SUFFIX_NOISE = re.compile(
    r"(?i)^(?:com|net|org|tv|me|io|cc|co|cn)$"
)
_RELEASE_GROUP_SUFFIX = re.compile(r"^[A-Za-z][A-Za-z0-9@._]{1,39}$")
_RELEASE_GROUP_SUFFIX_RESERVED = {
    "dl", "rip", "ray", "hdr", "uhd", "bit", "fps", "web", "webrip",
    "bluray", "bdrip", "remux", "hdtv", "aac", "flac", "dts", "hevc",
    "avc", "x264", "x265", "h264", "h265", "op", "ed", "ova", "oad",
    "sp", "ncop", "nced",
}
_PLAIN_EPISODE_SUFFIX = re.compile(r"(?i)(?:^|[ ._-])(\d{1,4})(?:v\d+)?\s*$")






_EXPLICIT_TMDB_WRAPPED_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"(?:\{tmdb-([0-9]{1,10})\}|\(tmdb-([0-9]{1,10})\))"
    r"(?![A-Za-z0-9])"
)
_EXPLICIT_TMDB_BARE_RE = re.compile(
    r"(?i)(?:^|(?<=[\s._()\[\]{}【】]))"
    r"(?:tmdb(?:[ +\-]?)([0-9]{1,10})|tdmb(?:[ +\-]+)([0-9]{1,10}))"
    r"(?=$|(?=[\s._()\[\]{}【】]))"
)














def _low_information_query(value: str) -> bool:
    """识别不足以支撑自动归档的极短标题，但仍允许展示候选供人工确认。"""
    compact = _comparison_key(value).replace(" ", "")
    if not compact or len(compact) <= 1:
        return True
    if re.fullmatch(r"[a-z0-9]{1,2}", compact):
        return True
    if re.fullmatch(r"\d{1,3}", compact):
        return True
    return False


def _similarity_score(query: str, candidate: str) -> float:
    left, right = _comparison_key(query), _comparison_key(candidate)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    compact_left = left.replace(" ", "")
    compact_right = right.replace(" ", "")
    if compact_left == compact_right:
        return 1.0
    if compact_left in compact_right or compact_right in compact_left:
        ratio = min(len(compact_left), len(compact_right)) / max(len(compact_left), len(compact_right))
        return round(0.78 + 0.17 * ratio, 3)
    token_left, token_right = set(left.split()), set(right.split())
    token_score = len(token_left & token_right) / len(token_left | token_right) if token_left and token_right else 0.0
    sequence_score = SequenceMatcher(None, compact_left, compact_right).ratio()
    return round(max(token_score, sequence_score * 0.9), 3)


@lru_cache(maxsize=2048)
def _han_pinyin_key(value: str) -> str:
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError:
        return ""
    return "".join(lazy_pinyin(value, style=Style.NORMAL))


def _han_script_similarity(query: str, candidate: str, direct_score: float) -> float:
    """用拼音为长中文标题提供受限的繁简辅助证据。

    仅处理两侧均为纯汉字的长标题，并要求字符层相似度已有一定基础。这样可以
    修复繁简体字形差异，同时避免把混合标题中的短中文前缀当成完整标题命中。
    """
    left = _comparison_key(query).replace(" ", "")
    right = _comparison_key(candidate).replace(" ", "")
    if (
        direct_score < 0.5
        or len(left) < 6
        or len(right) < 6
        or not re.fullmatch(r"[\u3400-\u9fff]+", left)
        or not re.fullmatch(r"[\u3400-\u9fff]+", right)
    ):
        return 0.0
    left_pinyin = _han_pinyin_key(left)
    right_pinyin = _han_pinyin_key(right)
    if not left_pinyin or not right_pinyin:
        return 0.0
    ratio = SequenceMatcher(None, left_pinyin, right_pinyin).ratio()
    if ratio < 0.9:
        return 0.0
    # 拼音属于辅助证据，不给满分，确保字形一致的完整标题仍优先。
    return round(min(0.98, ratio * 0.98), 3)


def _title_similarity_score(query: str, candidate: str) -> float:
    direct_score = _similarity_score(query, candidate)
    return max(direct_score, _han_script_similarity(query, candidate, direct_score))


_EXPLICIT_QUOTED_TITLE_WRAPPER = re.compile(
    r"^\s*(?P<wrapper>[A-Za-z][A-Za-z0-9 ._+\-]{1,31})\s*"
    r"[「『《](?P<title>[^「」『』《》]{2,120})[」』》]\s*$"
)
_EXPLICIT_QUOTED_GENERIC_WRAPPER_TOKENS = {
    "tv", "op", "ed", "movie", "film", "official", "channel", "trailer",
    "review", "preview", "news", "clip", "teaser",
}


def _explicit_quoted_title_matches(
    primary_queries: list[str], matched_query: str,
) -> bool:
    """确认短标题来自显式作品引号，而非任意删词得到的弱片名。

    ``Animatica「北斗之拳 …」`` 这类发布标题把栏目/包装名放在作品引号外。
    只有完整主锚点满足“短拉丁包装 + 明确 CJK 作品引号”，且引号内标题与
    TMDB 命中标题近乎精确时才放行；普通前后缀、季名和派生作品仍维持严格
    的显著残片门禁。
    """
    if not matched_query or _low_information_query(matched_query):
        return False
    for primary in primary_queries:
        wrapper = _EXPLICIT_QUOTED_TITLE_WRAPPER.fullmatch(str(primary or "").strip())
        if not wrapper:
            continue
        wrapper_tokens = re.findall(r"[A-Za-z0-9]+", wrapper.group("wrapper"))
        if not 1 <= len(wrapper_tokens) <= 3:
            continue
        if any(
            token.casefold() in _EXPLICIT_QUOTED_GENERIC_WRAPPER_TOKENS
            for token in wrapper_tokens
        ):
            continue
        inner_title = wrapper.group("title").strip()
        if _title_similarity_score(inner_title, matched_query) >= 0.97:
            return True
    return False


def _has_distinctive_title_remainder(primary_queries: list[str], matched_query: str) -> bool:
    """判断拆分短标题是否遗漏了完整标题中的显著片段。"""
    matched = _comparison_key(matched_query).replace(" ", "")
    if not matched:
        return False
    for primary in primary_queries:
        full = _comparison_key(primary).replace(" ", "")
        if not full or full == matched or matched not in full:
            continue
        remainder = full.replace(matched, "", 1)
        cjk_count = len(re.findall(r"[\u3400-\u9fff]", remainder))
        latin_tokens = re.findall(r"[a-z0-9]+", remainder)
        if cjk_count >= 2 or any(len(token) >= 4 for token in latin_tokens):
            return True
    return False


_PROTECTED_SHORT_TITLE_SUFFIX_RE = re.compile(
    r"(?i)(?:\s+|(?<=[\u3400-\u9fff]))"
    r"(xii|xi|x|ix|viii|vii|vi|iv|iii|ii|v)$"
)


def _has_protected_short_title_suffix(
    primary_queries: list[str], candidate_values: list[str],
) -> bool:
    """保护作品名末尾的单字母/罗马数字，不让高相似基础标题吞掉它。

    ``凸变英雄X``、``Kamen Rider X``、``Title II`` 的末尾标记虽短，
    却可能是作品身份的一部分。常规“显著残片”门禁会忽略单字符，因此
    这里仅在候选强命中去掉后缀的基础标题、且没有任一官方标题完整覆盖
    来源时触发负证据。
    """
    candidates = [value for value in candidate_values if value]
    if not candidates:
        return False
    for primary in primary_queries:
        normalized = _comparison_key(primary).strip()
        suffix_match = _PROTECTED_SHORT_TITLE_SUFFIX_RE.search(normalized)
        if not suffix_match:
            continue
        if max(
            (_title_similarity_score(primary, candidate) for candidate in candidates),
            default=0.0,
        ) >= 0.97:
            continue
        base = normalized[:suffix_match.start()].strip()
        if len(base.replace(" ", "")) < 2:
            continue
        if max(
            (_title_similarity_score(base, candidate) for candidate in candidates),
            default=0.0,
        ) >= 0.92:
            return True
    return False


def _known_season_alias_remainder(
    primary_queries: list[str], matched_query: str, season: int | None,
) -> bool:
    """确认完整标题相对基础标题只多出已解析季号别名。"""
    if season is None or season <= 0:
        return False
    matched = _comparison_key(matched_query).replace(" ", "")
    if not matched:
        return False
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(season, "th")
    allowed = {
        f"season{season}",
        f"{season}{suffix}season",
    }
    # ``2nd Attack`` 是少数动画续季的官方季别名，但也可能是作品标题的一部分。
    # 仅对具有足够中文锚点的基础标题放行，避免 ``Art Attack 2nd Attack``
    # 之类纯拉丁标题被错误折叠到基础作品。
    if len(re.findall(r"[\u3400-\u9fff]", matched)) >= 4:
        allowed.add(f"{season}{suffix}attack")
    for primary in primary_queries:
        full = _comparison_key(primary).replace(" ", "")
        if not full or matched not in full:
            continue
        remainder = full.replace(matched, "", 1)
        if remainder in allowed:
            return True
    return False


def _ambiguous_latin_ordinal_attack_alias(
    primary_queries: list[str], candidate_values: list[str], season: int | None,
) -> bool:
    """识别纯拉丁标题末尾的 ``2nd Attack`` 歧义，强制转人工确认。"""
    if season is None or season <= 0:
        return False
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(season, "th")
    alias = f"{season}{suffix}attack"
    for candidate in candidate_values:
        base = _comparison_key(candidate).replace(" ", "")
        if not base or re.search(r"[\u3400-\u9fff]", base):
            continue
        for primary in primary_queries:
            full = _comparison_key(primary).replace(" ", "")
            if full and full.replace(base, "", 1) == alias:
                return True
    return False


def _exact_latin_pinyin_equivalent(source_part: str, candidate_value: str) -> bool:
    """只把完整拉丁拼音视为同一中文标题的辅助覆盖证据。

    来源常同时写 ``仙逆 Xian Ni``，而 TMDB 只提供中文正式名。这里要求
    拉丁片段至少包含两个词、去分隔符后不少于六个字符，并与候选纯中文
    标题的完整拼音逐字相等。该证据只参与双语标题的“每段均已覆盖”校验，
    不会让单独的拼音查询绕过正常候选消歧。
    """
    latin = _comparison_key(source_part)
    tokens = re.findall(r"[a-z]+", latin)
    if len(tokens) < 2 or " ".join(tokens) != latin:
        return False
    compact_latin = "".join(tokens)
    if len(compact_latin) < 6:
        return False

    candidate = _comparison_key(candidate_value).replace(" ", "")
    if not candidate or not re.fullmatch(r"[\u3400-\u9fff]+", candidate):
        return False
    candidate_pinyin = _han_pinyin_key(candidate).casefold()
    return bool(candidate_pinyin and compact_latin == candidate_pinyin)


def _primary_title_parts_covered(primary_queries: list[str], candidate_values: list[str]) -> bool:
    """候选的标题、原名和别名可共同覆盖双语完整标题。

    TMDB 的 ``original_name`` 偶尔会把拉丁标题和日文标题写在同一个字段里，
    例如 ``Übel Blatt～ユーベルブラット～``。来源标题则常写成
    ``魔域英雄传说 Ubel Blatt``。覆盖校验既要看到中文正式名，也要从原名中
    提取出受变音符号影响的拉丁片名；若拉丁片段是同一中文正式名的完整
    拼音，也可作为严格等价证据。但仍要求来源的每个有效标题片段都由 TMDB
    官方标题、原名或别名解释，不能凭发布名中的任意英文残片放行。
    """
    coverage_values: list[str] = list(candidate_values)
    for value in candidate_values:
        normalized = unicodedata.normalize("NFKD", str(value or ""))
        normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
        coverage_values.extend(_split_title_variants(normalized))
    coverage_values = _unique_text(coverage_values)

    for primary in primary_queries:
        parts = [
            part for part in _split_title_variants(primary)
            if _comparison_key(part) != _comparison_key(primary)
            and not _low_information_query(part)
        ]
        if len(parts) < 2:
            continue
        if all(
            max(
                (_title_similarity_score(part, value) for value in coverage_values if value),
                default=0.0,
            ) >= 0.9
            or any(
                _exact_latin_pinyin_equivalent(part, value)
                for value in coverage_values
                if value
            )
            for part in parts
        ):
            return True
    return False


def _verify_source_title_anchor(
    source_anchors: list[str],
    candidate_titles: list[str],
    *,
    season: int | None,
) -> tuple[bool, float, str, str, str]:
    """验证候选是否解释了来源标题，而不是只命中通用前缀。

    LLM 只能提供查询修正，不能把来源中的独特作品标识吞掉。例如
    ``Kamen Rider ZEZTZ`` 命中 ``Kamen Rider`` 时字符串分数仍可能很高，
    但 ``ZEZTZ`` 没有被 TMDB 标题、原名或官方别名解释，必须保持人工确认。
    """
    scores = [
        (_title_similarity_score(source, candidate), source, candidate)
        for source in source_anchors
        for candidate in candidate_titles
        if source and candidate
    ]
    score, matched_source, matched_candidate = max(
        scores, default=(0.0, "", ""), key=lambda item: item[0]
    )
    if not source_anchors:
        return False, score, matched_source, matched_candidate, "source_anchor_missing"
    if score < 0.72:
        return False, score, matched_source, matched_candidate, "source_anchor_unverified"
    protected_suffix_missing = _has_protected_short_title_suffix(
        [matched_source], candidate_titles
    )
    if protected_suffix_missing:
        return (
            False,
            score,
            matched_source,
            matched_candidate,
            "protected_source_title_suffix_missing",
        )
    distinctive_remainder = bool(
        _has_distinctive_title_remainder([matched_source], matched_candidate)
        and not _explicit_quoted_title_matches(
            [matched_source], matched_candidate,
        )
        and not _known_season_alias_remainder(
            [matched_source], matched_candidate, season,
        )
        and not _primary_title_parts_covered(source_anchors, candidate_titles)
    )
    if distinctive_remainder:
        return (
            False,
            score,
            matched_source,
            matched_candidate,
            "distinctive_source_title_remainder",
        )
    return True, score, matched_source, matched_candidate, "verified"


def _source_title_anchors(context: RecognitionContext | None) -> list[str]:
    """提取可用于最终放行的来源标题锚点。

    这里只读取文件名、父目录在进入管理员预处理规则前解析出的主标题，
    不纳入为了提高召回率拆出的短变体。这样 BGM/豆瓣、AI、Tavily 可以
    帮忙找到候选，但不能用自己生成的标题反向证明自己。
    """
    if context is None:
        return []
    return [
        value
        for value in _unique_text((
            context.normalized_title,
            context.filename_title,
            context.folder_title,
        ))
        if value and not _low_information_query(value)
    ]
















_ROMAN_SEASON_NUMBERS = {
    "II": 2,
    "III": 3,
    "IV": 4,
    "V": 5,
    "VI": 6,
    "VII": 7,
    "VIII": 8,
    "IX": 9,
    "X": 10,
    "Ⅱ": 2,
    "Ⅲ": 3,
    "Ⅳ": 4,
    "Ⅴ": 5,
    "Ⅵ": 6,
    "Ⅶ": 7,
    "Ⅷ": 8,
    "Ⅸ": 9,
    "Ⅹ": 10,
}


def _implicit_season_body_is_safe(value: str) -> bool:
    """判断续作数字前是否有足够稳定的作品标题主体。"""
    body = re.sub(r"\s+", " ", str(value or "")).strip(" ._-")
    if not body or _AMBIGUOUS_SEQUEL_PREDECESSOR.search(body):
        return False
    cjk_count = len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff]", body))
    latin_tokens = re.findall(r"[A-Za-z][A-Za-z0-9']*", body)
    # 对短标题宁可保留人工确认；避免把 ``86``、``Part 2`` 等正式标题
    # 成分误判成季号。正式片名尾部的罗马数字还需由发布结构证据约束。
    return cjk_count >= 4 or len(latin_tokens) >= 3


def _implicit_cjk_subtitle_season_is_safe(
    source: str, match: re.Match[str],
) -> bool:
    """校验 ``短标题 Ⅱ－长 CJK 副标题－ - 集号`` 的窄例外。"""
    body = re.sub(
        r"\s+", " ", str(source or "")[:match.start("season")]
    ).strip(" ._-—–－:：")
    if not body or _AMBIGUOUS_SEQUEL_PREDECESSOR.search(body):
        return False
    body_cjk = len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff]", body))
    body_latin = re.findall(r"[A-Za-z][A-Za-z0-9']*", body)
    subtitle = str(match.groupdict().get("subtitle") or "")
    subtitle_cjk = len(
        re.findall(r"[\u3040-\u30ff\u3400-\u9fff]", subtitle)
    )
    return bool((body_cjk >= 2 or body_latin) and subtitle_cjk >= 4)


def _implicit_season_has_release_evidence(
    source: str, match: re.Match[str],
) -> bool:
    """隐式季号必须同时具备发布结构证据，不能只凭标题尾部数字。"""
    if match.groupdict().get("volume"):
        return True
    tail = str(source or "")[match.end("season"):]
    if _IMPLICIT_SEASON_TECHNICAL_EVIDENCE.search(tail):
        return True
    return any(
        _is_bracket_noise(item.group(1))
        for item in _BRACKETED_SEGMENT.finditer(tail)
    )


def _implicit_season_hint(
    text: str, *, episode_context: bool = False,
) -> tuple[int | None, tuple[int, int] | None]:
    """保守提取发布名尾部的隐式续作季号及应从标题移除的范围。

    仅处理 ``Title 3 - 06``、``Title II - 06``，以及父目录
    ``Title 2 Vol.1`` + 子文件已有集号的结构。``Part/Cour/Disc`` 等
    分部语义不会自动换算成 TMDB 季号。
    """
    if not episode_context:
        return None, None
    source = str(text or "")
    matches = [
        *(_IMPLICIT_SEASON_EPISODE_TOKEN.finditer(source)),
        *(_IMPLICIT_SEASON_BRACKET_EPISODE_TOKEN.finditer(source)),
        *(_IMPLICIT_CJK_SUBTITLE_SEASON_EPISODE_TOKEN.finditer(source)),
        *(_IMPLICIT_SEASON_VOLUME_TOKEN.finditer(source)),
        *(_ROMAJI_SECOND_SEASON_EPISODE_TOKEN.finditer(source)),
        *(_PUNCTUATED_NUMERIC_SEASON_EPISODE_TOKEN.finditer(source)),
    ]
    for match in sorted(matches, key=lambda item: item.start("season"), reverse=True):
        has_cjk_subtitle = bool(match.groupdict().get("subtitle"))
        if has_cjk_subtitle:
            body_is_safe = _implicit_cjk_subtitle_season_is_safe(source, match)
        else:
            body_is_safe = _implicit_season_body_is_safe(
                source[:match.start("season")]
            )
        if not body_is_safe:
            continue
        if not _implicit_season_has_release_evidence(source, match):
            continue
        raw = str(match.group("season") or "").upper()
        # 单字母 ``X`` 在特摄、游戏和动画标题中经常是作品身份的一部分，
        # 不能仅凭后续方括号集号推断为第 10 季。明确的 S10/Season 10 和
        # 数字 ``10`` 仍按既有规则解析；罕见的纯罗马数字第十季转人工更安全。
        if raw == "X":
            continue
        season = (
            int(raw)
            if raw.isdigit()
            else 2 if raw == "NI"
            else _ROMAN_SEASON_NUMBERS.get(raw)
        )
        if season is None or not 2 <= season <= 20:
            continue
        if match.groupdict().get("subtitle"):
            end = match.end("subtitle")
        elif match.groupdict().get("volume"):
            end = match.end("volume")
        elif match.groupdict().get("marker"):
            end = match.end("marker")
        else:
            end = match.end("season")
        return season, (match.start("season"), end)
    return None, None


def _remove_text_span(value: str, span: tuple[int, int] | None) -> str:
    if not span:
        return str(value or "")
    start, end = span
    source = str(value or "")
    if start < 0 or end <= start or start >= len(source):
        return source
    return f"{source[:start]} {source[min(end, len(source)):]}"










def _extract_season(text: str, *, episode_context: bool = False) -> int | None:
    explicit = _extract_explicit_season(text, episode_context=episode_context)
    if explicit is not None:
        return explicit
    implicit, _ = _implicit_season_hint(text, episode_context=episode_context)
    if implicit is not None:
        return implicit
    return None


def _contains_unresolved_season_hint(value: str) -> bool:
    """只把带作品标题主体的 Part/Cour 等结构视为未决季号。"""
    source = str(value or "").strip()
    match = _UNRESOLVED_SEQUEL_HINT.search(source)
    if not match:
        return False
    body = f"{source[:match.start()]} {source[match.end():]}"
    body_episode = _extract_episode(body)
    body = _strip_known_episode_suffix(
        body,
        body_episode,
        _extract_season(body, episode_context=body_episode is not None),
    )
    body, _ = _clean_release_stem(body)
    cjk_count = len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff]", body))
    latin_tokens = re.findall(r"[A-Za-z][A-Za-z0-9']*", body)
    return cjk_count >= 2 or len(latin_tokens) >= 2


def has_unresolved_season_hint(filename: str, parent_path: str = "") -> bool:
    """检测有续作/分部提示但无法安全换算季号的剧集发布名。

    父路径只检查最邻近的媒体目录，避免 ``/downloads/Part 2/Normal Show``
    这类上层分类目录误伤正常 S01 回落。
    """
    raw = str(filename or "")
    stem = raw.rsplit(".", 1)[0] if "." in raw else raw
    episode = _extract_episode(stem)
    if episode is None or _extract_season(stem, episode_context=True) is not None:
        return False
    if _contains_unresolved_season_hint(stem):
        return True
    parts = [
        part.strip()
        for part in re.split(r"[\\/]+", str(parent_path or ""))
        if part.strip()
    ]
    for part in reversed(parts):
        if _GENERIC_FOLDER.fullmatch(part):
            continue
        return _contains_unresolved_season_hint(part)
    return False


def _strip_season_tokens(value: str) -> str:
    source = str(value or "")
    if _SEASON_RANGE_TOKEN.search(source):
        return source
    cleaned = _CHINESE_SEASON_COMPLETION_TOKEN.sub(" ", source)
    cleaned = _CHINESE_ANNUAL_RELEASE_TOKEN.sub(" ", cleaned)
    cleaned = _ORDINAL_SEASON_TOKEN.sub(" ", cleaned)
    cleaned = _ENGLISH_ORDINAL_SEASON_TOKEN.sub(" ", cleaned)
    cleaned = _SEASON_TOKEN.sub(" ", cleaned)
    cleaned = _BRACKET_TV_SEASON_TOKEN.sub(" ", cleaned)
    return _CHINESE_SEASON_TOKEN.sub(" ", cleaned)









_GUESSIT_CJK_EPISODE_WORD_COLLISION = re.compile(
    r"第\s*(?:\d{1,4}|[零〇一二两三四五六七八九十]{1,3})\s*"
    r"(?:集|[话話])(?=[\u3040-\u30ff\u3400-\u9fff])"
)




def _strip_known_episode_suffix(
    value: str,
    episode: int | None,
    season: int | None = None,
) -> str:
    """只移除已由解析器确认的尾部集号，避免把片名中的数字误删。"""
    if episode is None:
        return str(value or "")

    def replace(match: re.Match) -> str:
        try:
            return " " if int(match.group(1)) == int(episode) else match.group(0)
        except (TypeError, ValueError):
            return match.group(0)

    cleaned = str(value or "")
    compact = _parse_release_x_position(cleaned)
    if compact is not None:
        compact_season, compact_episode, compact_span = compact
        if (
            compact_episode == int(episode)
            and (season is None or compact_season == int(season))
        ):
            cleaned = _remove_text_span(cleaned, compact_span)
    cleaned = _BARE_EPISODE_SUFFIX.sub(replace, cleaned)
    cleaned = _BRACKET_EPISODE_TOKEN.sub(replace, cleaned)
    return _PLAIN_EPISODE_SUFFIX.sub(replace, cleaned)


_UNLABELED_EPISODE_RESERVED_VALUES = {
    360, 480, 576, 720, 1080, 1440, 2160, 4320,
}







def parse_release_position(value: str) -> dict[str, int | None]:
    """从发布标题中提取可安全展示/排序的季集位置，不发起任何网络请求。"""
    name = _strip_explicit_tmdb_markers(str(value or ""))
    stem = name.rsplit(".", 1)[0] if "." in name else name
    episode = _extract_episode(stem)
    season = _extract_season(stem, episode_context=episode is not None)
    compact = _parse_release_x_position(stem)
    if compact is not None:
        season = season if season is not None else compact[0]
        episode = episode if episode is not None else compact[1]
    episode_end = None
    special_range = _SPECIAL_EPISODE_RANGE.search(stem)
    if special_range:
        start, end = (int(item) for item in special_range.groups())
        if 1 <= start <= end <= 500:
            season, episode, episode_end = 0, start, end
    else:
        special_episode = _extract_special_episode(stem)
        if special_episode is not None:
            season, episode = 0, special_episode

    match = _RELEASE_EPISODE_RANGE.search(stem)
    if match:
        groups = [int(item) for item in match.groups() if item]
        if len(groups) >= 2:
            start, end = groups[0], groups[1]
            if 0 <= start <= 500 and start <= end <= 500:
                episode = start
                episode_end = end
    elif completed := _BARE_COMPLETED_EPISODE_RANGE.search(stem):
        start, end = (int(item) for item in completed.groups())
        if (
            _valid_unlabeled_episode(start)
            and _valid_unlabeled_episode(end)
            and start <= end <= 500
        ):
            episode = start
            episode_end = end
    return {"season": season, "episode": episode, "episode_end": episode_end}






def _bracket_content(token: str) -> str:
    text = str(token or "").strip()
    if len(text) >= 2 and text[0] in "[【" and text[-1] in "]】":
        return text[1:-1].strip()
    return text


def _strip_trailing_checksum(value: str) -> str:
    """在 GuessIt/年份提取前移除尾部 CRC，避免十六进制片段伪装成年份。"""
    text = str(value or "")
    checksum = _CHECKSUM_SUFFIX.search(text)
    return text[:checksum.start()].rstrip() if checksum else text


def _is_valid_bracket_release_date(value: str) -> bool:
    match = _BRACKET_RELEASE_DATE.fullmatch(str(value or "").strip())
    if not match:
        return False
    try:
        date(int(match.group(1)), int(match.group(3)), int(match.group(4)))
    except ValueError:
        return False
    return True


def _is_valid_release_date_match(match: re.Match[str]) -> bool:
    try:
        date(int(match.group(1)), int(match.group(3)), int(match.group(4)))
    except ValueError:
        return False
    return True


def _is_explicit_subtitle_language_noise(value: str) -> bool:
    compact = re.sub(r"\s+", "", str(value or "")).strip()
    return bool(
        compact
        and _EXPLICIT_SUBTITLE_MARKER.search(compact)
        and _EXPLICIT_SUBTITLE_LANGUAGE.search(compact)
        and _EXPLICIT_SUBTITLE_ALLOWED.fullmatch(compact)
    )


def _is_bracket_noise(content: str) -> bool:
    compact = re.sub(r"\s+", " ", str(content or "")).strip()
    if not compact or _BRACKET_EPISODE_RANGE.fullmatch(compact):
        return True
    if (
        _is_valid_bracket_release_date(compact)
        or _is_explicit_subtitle_language_noise(compact)
    ):
        return True
    if (
        _RELEASE_KIND_VERSION_BRACKET.fullmatch(compact)
        or _RELEASE_LANGUAGE_BRACKET.fullmatch(compact)
        or _BRACKET_DUB_AUDIO_NOISE.fullmatch(compact)
        or _BRACKET_RELEASE_EDITION_NOISE.fullmatch(compact)
        or _BRACKET_RELEASE_META_NOISE.fullmatch(compact)
    ):
        return True
    token_source = compact
    # 频道/播出源缩写本身可能是标题的一部分，只有同一括号同时包含
    # “台标 + 分辨率 + 编解码”三类证据时才把台标归一化为技术噪声。
    # 这样既可清理 ``(BS-NTV 1440x1080 MPEG2 AAC)``，也可处理
    # ``(B-Global Donghua 1920x832 HEVC AAC MKV)``；单独的 Donghua
    # 或不含完整技术证据的括号仍会保留，避免误删正式标题。
    broadcaster_prefix = re.match(
        r"(?i)^(?:B[ ._-]?GLOBAL(?:[ ._-]+DONGHUA)?|"
        r"BS[ ._-]?(?:NTV|11)|AT[ ._-]?X|NBN(?:[ ._-]?TV)?|"
        r"NTV|NHK|TBS|TOKYO[ ._-]?MX|MX)(?=$|[\s,;+/＆&._-]+)",
        token_source,
    )
    if broadcaster_prefix:
        technical_tail = token_source[broadcaster_prefix.end():]
        has_resolution = bool(re.search(
            r"(?i)(?:\b(?:2160|1440|1080|720|576|480)p\b|"
            r"(?<!\d)\d{3,4}x\d{3,4}(?!\d))",
            technical_tail,
        ))
        has_codec = bool(re.search(
            r"(?i)\b(?:mpeg[ ._-]?2|h[ ._-]?26[45]|x26[45]|hevc|avc|"
            r"aac|ac3|eac3|ddp|dts|flac|opus)\b",
            technical_tail,
        ))
        if has_resolution and has_codec:
            token_source = f"tv {technical_tail}"
    for pattern, replacement in (
        (r"(?i)\bweb[ ._-]?dl\b", "webdl"),
        (r"(?i)\bweb[ ._-]?rip\b", "webrip"),
        (r"(?i)\bblu[ ._-]?ray\b", "bluray"),
        (r"(?i)\bh[ ._-]?26([45])\b", r"h26\1"),
        (r"(?i)\b10[ ._-]?bit\b", "10bit"),
        (r"(?i)\b8[ ._-]?bit\b", "8bit"),
        (r"(?i)\bdual[ ._-]+audio\b", "audio"),
        (r"(?i)\b(?:ddp|eac3|ac3|aac|dts|flac|opus)[ ._-]?(?:1|2|5|7)[ ._-]?[01]\b", "aac"),
    ):
        token_source = re.sub(pattern, replacement, token_source)
    tokens = [item for item in re.split(r"[\s,;+/＆&._-]+", token_source) if item]
    if not tokens:
        return False
    # 流媒体平台名只有在同一括号还包含来源/分辨率/编码等技术证据时
    # 才属于发布元数据。单独的 ``[WETV]`` 或 ``[WETV Original]``
    # 仍可能是标题/系列品牌，必须保留。
    has_technical_token = any(
        (_NOISE.fullmatch(item) and not _BRACKET_STREAMING_PLATFORM_NOISE.fullmatch(item))
        or _BRACKET_SHORT_LANGUAGE_NOISE.fullmatch(item)
        or _BRACKET_RELEASE_META_NOISE.fullmatch(item)
        or _BRACKET_DIMENSION_NOISE.fullmatch(item)
        for item in tokens
    )
    return has_technical_token and all(
        _NOISE.fullmatch(item)
        or _BRACKET_SHORT_LANGUAGE_NOISE.fullmatch(item)
        or _BRACKET_RELEASE_META_NOISE.fullmatch(item)
        or _BRACKET_DIMENSION_NOISE.fullmatch(item)
        or _BRACKET_STREAMING_PLATFORM_NOISE.fullmatch(item)
        or _BRACKET_DOMAIN_SUFFIX_NOISE.fullmatch(item)
        # 年份本身不构成发布噪声；只有同一括号已出现来源、编码、
        # 分辨率等技术证据时，才允许它作为附属 token 随整体移除。
        or _YEAR_TOKEN.fullmatch(item)
        or (len(tokens) > 1 and _BRACKET_LONG_LANGUAGE_NOISE.fullmatch(item))
        for item in tokens
    )


def _probable_unknown_release_prefix(content: str, remainder: str) -> bool:
    """识别结构明确、但尚未进入知识库的首段发布组。

    这里只生成更干净的候选标题，不把原始内容永久丢弃。除了长度差，
    还要求前缀具备发布组织结构信号；不能仅因后方标题较长就把 ``[The]``、
    ``[86]`` 等真实标题片段误判为制作组。
    """
    prefix = re.sub(r"\s+", " ", str(content or "")).strip()
    suffix = re.sub(r"\s+", " ", str(remainder or "")).strip()
    if not prefix or not suffix or len(prefix) > 40:
        return False
    if re.search(r"[^A-Za-z0-9 '&+._-]", prefix):
        return False
    prefix_tokens = re.findall(r"[A-Za-z0-9]+", prefix)
    suffix_tokens = re.findall(r"[A-Za-z0-9\u3040-\u30ff\u3400-\u9fff]+", suffix)
    if not 1 <= len(prefix_tokens) <= 4 or len(suffix_tokens) < 3:
        return False
    if not any(re.search(r"[A-Za-z]", token) for token in prefix_tokens):
        return False
    group_marker = re.search(
        r"(?i)(?:^|[\s._&+-])(?:studio|team|group|fansub|sub|subs|raw|raws|"
        r"encode|enc|rip|origin|house|works|project)(?:$|[\s._&+-])",
        prefix,
    )
    compact = re.sub(r"[^A-Za-z0-9]", "", prefix)
    compact_group_marker = bool(re.search(
        r"(?i)(?:studio|team|group|fansub|subs?|raws?|encode|enc|rip|"
        r"origin|house|works|project)$",
        compact,
    ))
    acronym_group = bool(
        2 <= len(compact) <= 16
        and re.fullmatch(r"[A-Z][A-Z0-9]+", compact)
        and re.search(r"[A-Z]", compact)
    )
    bracket_noise_count = sum(
        1 for match in _BRACKETED_SEGMENT.finditer(suffix)
        if _is_bracket_noise(match.group(1))
    )
    structured_release = bool(
        bracket_noise_count >= 1
        and len(prefix_tokens) == 1
        and compact.casefold() not in {
            "a", "an", "the", "movie", "film", "part", "cour", "chapter",
            "vol", "volume", "disc", "disk", "final",
        }
        and 4 <= len(compact) <= 24
    )
    if not group_marker and not compact_group_marker and not acronym_group and not structured_release:
        return False
    prefix_size = len(_comparison_key(prefix).replace(" ", ""))
    suffix_size = len(_comparison_key(suffix).replace(" ", ""))
    return suffix_size >= max(8, prefix_size + 4)


def _non_destructive_release_title_candidates(
    value: str,
) -> tuple[list[str], dict[str, list[str]]]:
    """从发布名生成保守的额外标题候选，并保留原始清洗结果。

    未知发布组不会直接写进全局正则；只有当前文件结构足够明确时，才把
    去掉首段方括号后的标题加入候选列表。括号中的媒介/修订/语言标签则
    作为结构化发布元数据记录，供诊断和后续学习使用。
    """
    source = str(value or "").strip()
    candidates: list[str] = []
    components = {
        "candidate_release_groups": [],
        "media_kinds": [],
        "language_tags": [],
        "release_versions": [],
    }
    structured_position = _STRUCTURED_EPISODE_POSITION.search(source)
    compact_position = _parse_release_x_position(source)
    position_start = min(
        (
            start
            for start in (
                structured_position.start() if structured_position else None,
                compact_position[2][0] if compact_position else None,
            )
            if start is not None
        ),
        default=None,
    )
    if position_start is not None:
        projected, _ = _clean_release_stem(source[:position_start])
        projected = re.sub(
            r"\s+", " ", _strip_season_tokens(projected)
        ).strip(" ._-")
        if projected and not _low_information_query(projected):
            candidates.append(projected)
    first = _RELEASE_PREFIX.match(source)
    if first:
        content = _bracket_content(first.group(1))
        remainder = source[first.end():].lstrip()
        if (
            not _is_release_prefix(content, remainder)
            and _probable_unknown_release_prefix(content, remainder)
        ):
            projected, _ = _clean_release_stem(remainder)
            projected = re.sub(
                r"\s+", " ", _strip_season_tokens(projected)
            ).strip(" ._-")
            if projected and not _low_information_query(projected):
                # 已通过保守结构判定的未知发布组，其去前缀标题应成为主锚点；
                # 原始含发布组候选仍保留在后续 variants 中用于搜索召回。
                candidates.insert(0, projected)
                components["candidate_release_groups"].append(content)

    tail_group = _candidate_tail_release_group(source)
    if tail_group:
        group_name, split_at = tail_group
        projected, _ = _clean_release_stem(source[:split_at])
        projected = re.sub(
            r"\s+", " ", _strip_season_tokens(projected)
        ).strip(" ._-")
        if projected and not _low_information_query(projected):
            candidates.insert(0, projected)
            components["candidate_release_groups"].append(group_name)

    for match in _BRACKETED_SEGMENT.finditer(source):
        content = match.group(1).strip()
        if _RELEASE_KIND_VERSION_BRACKET.fullmatch(content):
            components["media_kinds"].append(content)
            revision = re.search(r"(?i)\bv\d{1,3}\b", content)
            if revision:
                components["release_versions"].append(revision.group(0))
        elif _RELEASE_LANGUAGE_BRACKET.fullmatch(content):
            components["language_tags"].append(content)
    return _unique_text(candidates), {
        key: _unique_text(values) for key, values in components.items()
    }


def _known_recognition_value(value: str, knowledge_type: str) -> bool:
    try:
        from app.modules.recognition_knowledge import is_known

        return is_known(value, knowledge_type)
    except Exception as exc:
        logger.debug("本地识别词库查询失败 type=%s", type(exc).__name__)
        return False


def _is_known_release_group(value: str) -> bool:
    return bool(_RELEASE_GROUP_BRACKET.fullmatch(value)) or _known_recognition_value(
        value, "release_group"
    )


def _is_release_prefix(content: str, remainder: str) -> bool:
    compact = re.sub(r"\s+", " ", str(content or "")).strip()
    compound_groups = [
        item.strip()
        for item in re.split(r"\s*(?:&|＆|\+|/|、)\s*", compact)
        if item.strip()
    ]
    known_compound_group = (
        len(compound_groups) >= 2
        and all(_is_known_release_group(item) for item in compound_groups)
    )
    return bool(
        _is_bracket_noise(compact)
        or _is_known_release_group(compact)
        or _RELEASE_SOURCE_BRACKET.fullmatch(compact)
        or known_compound_group
    )


_CJK_RELEASE_GROUP_SUFFIX = re.compile(
    r"(?:字幕组|字幕組|制作组|製作組|压制组|壓制組|翻译组|翻譯組|汉化组|漢化組|发布组|發佈組)$"
)
_CJK_TAIL_RELEASE_GROUP = re.compile(
    r"(?P<separator>\s[-—–]\s)(?P<group>[\u3040-\u30ff\u3400-\u9fffA-Za-z0-9@._＆&+ -]{2,24})$"
)


def _candidate_tail_release_group(stem: str) -> tuple[str, int] | None:
    """识别中文/日文尾部发布组，但只生成候选，不直接删除未知标题。"""
    match = _CJK_TAIL_RELEASE_GROUP.search(str(stem or "").strip())
    if not match:
        return None
    candidate = re.sub(r"\s+", " ", match.group("group")).strip()
    if not candidate or _NOISE.fullmatch(candidate):
        return None
    if not (_CJK_RELEASE_GROUP_SUFFIX.search(candidate) or _is_known_release_group(candidate)):
        return None
    prefix = str(stem or "")[:match.start("separator")].strip()
    if not prefix or not (
        _STRUCTURED_EPISODE_POSITION.search(prefix)
        or _BRACKETED_SEGMENT.search(prefix)
        or _NOISE.search(prefix)
    ):
        return None
    return candidate, match.start("separator")


def _tail_release_group(stem: str) -> tuple[str, int] | None:
    """保守提取紧凑的尾部制作组，避免把 ``WEB-DL.1080p...`` 当作组名。"""
    split_at = str(stem or "").rfind("-")
    if split_at < 0:
        return None
    candidate = stem[split_at + 1:].strip()
    if not _RELEASE_GROUP_SUFFIX.fullmatch(candidate):
        return None
    if _known_recognition_value(candidate, "release_suffix"):
        return candidate, split_at
    parts = [part.lower() for part in re.split(r"[.@_]", candidate) if part]
    if not parts or any(
        part in _RELEASE_GROUP_SUFFIX_RESERVED or _NOISE.fullmatch(part)
        for part in parts
    ):
        return None
    if any(re.fullmatch(r"(?:\d{3,4}p|\d{1,2}bit|\d{1,3}fps|\d{1,2})", part) for part in parts):
        return None
    return candidate, split_at


def _clean_release_stem(value: str) -> tuple[str, dict[str, list[str]]]:
    stem = _AVAILABILITY_TAG.sub(" ", str(value or "")).strip()
    # Crunchyroll 常缩写为 CR/C R，仅在紧邻 WEBRip 时作为发布源清除，
    # 避免把标题中的普通字母组合误当噪声。
    stem = _CR_WEBRIP_SOURCE.sub(" ", stem)
    cleaned = {
        "release_prefixes": [],
        "checksums": [],
        "release_groups": [],
        "release_versions": [],
        "noise_tokens": [],
    }

    def strip_release_date(match: re.Match[str]) -> str:
        if not _is_valid_release_date_match(match):
            return match.group(0)
        cleaned["noise_tokens"].append(match.group(0))
        return " "

    # 日更节目与源站发布常把日期裸写在标题中。只移除通过日历校验的完整
    # yyyy-mm-dd/yyyy.mm.dd/yyyy/mm/dd，避免把普通年份或版本号当噪声。
    stem = _UNBRACKETED_RELEASE_DATE.sub(strip_release_date, stem)

    def strip_episode_range(match: re.Match[str]) -> str:
        cleaned["noise_tokens"].append(match.group(0).strip())
        return " "

    # 整季目录常把范围写成 ``S01E01-E16``；若只让通用噪声规则删除
    # ``S01E01``，尾部 ``16`` 会继续污染作品标题。
    stem = _RELEASE_EPISODE_RANGE.sub(strip_episode_range, stem)
    while True:
        match = _RELEASE_PREFIX.match(stem)
        if not match:
            break
        token = match.group(1)
        content = _bracket_content(token)
        remainder = stem[match.end():].lstrip()
        if not _is_release_prefix(content, remainder):
            # 标题本身被方括号包裹时只拆括号，不得连标题一起删除。
            stem = f"{content} {remainder}".strip()
            break
        cleaned["release_prefixes"].append(token)
        stem = remainder
    site = _SITE_PREFIX.match(stem)
    if site:
        cleaned["release_prefixes"].append(site.group(1))
        stem = stem[site.end():]
    checksum = _CHECKSUM_SUFFIX.search(stem)
    if checksum:
        checksum_value = checksum.group("bracket") or checksum.group("parenthesized")
        cleaned["checksums"].append(checksum_value)
        stem = stem[:checksum.start()]
    # 发布修订标记要在通用括号噪声清理前记录；否则 ``[v2]`` 会先被
    # 技术标签分类移除，诊断中却丢失版本信息。标题中的裸 ``V2`` 保留。
    cleaned["release_versions"] = _unique_text(
        match.group(1) for match in _RELEASE_REVISION_BRACKET.finditer(stem)
    )
    # 非前缀位置的整季范围与技术规格也必须整体移除。若只在最后删除
    # 括号，``[01-20 FIN]`` 会退化成 ``01 20 FIN`` 并污染搜索标题。
    def strip_bracket_noise(match: re.Match[str]) -> str:
        content = match.group(1).strip()
        if not _is_bracket_noise(content):
            return match.group(0)
        cleaned["noise_tokens"].append(content)
        return " "

    stem = _BRACKETED_SEGMENT.sub(strip_bracket_noise, stem)
    stem = _RELEASE_REVISION_BRACKET.sub(" ", stem)
    # ``(Complete)`` 常用于整季/全集发布状态；前面的技术括号被移除后，
    # 它会成为尾部孤立标记。只清理带括号且位于尾部的形式，避免误删
    # 真实片名中的 ``Complete``。裸 ``FINAL/FIN`` 继续沿用既有规则。
    bracket_completion = _TRAILING_BRACKET_RELEASE_COMPLETION.search(stem)
    if bracket_completion and re.search(
        r"[A-Za-z0-9\u3040-\u30ff\u3400-\u9fff]",
        stem[:bracket_completion.start()],
    ):
        cleaned["noise_tokens"].append(bracket_completion.group("tag"))
        stem = stem[:bracket_completion.start()]
    completion = _TRAILING_RELEASE_COMPLETION.search(stem)
    if completion and re.search(r"[A-Za-z0-9\u3040-\u30ff\u3400-\u9fff]", stem[:completion.start()]):
        cleaned["noise_tokens"].append(completion.group("tag"))
        stem = stem[:completion.start()]
    if _NOISE.search(stem):
        release_group = _tail_release_group(stem)
        if release_group:
            group_name, split_at = release_group
            cleaned["release_groups"].append(group_name)
            stem = stem[:split_at]

    def strip_release_language_name(match: re.Match[str]) -> str:
        cleaned["noise_tokens"].append(match.group("language"))
        return match.group("technical")

    # ``AAC.English.CHS-ENG`` 中的 English 是音轨语言而不是片名；只有它
    # 同时夹在技术标记与语言代码之间时才删除，不能全局清理 English。
    stem = _RELEASE_LANGUAGE_NAME_AFTER_TECH.sub(strip_release_language_name, stem)
    legacy_multi = _LEGACY_MULTI_TECH_TAIL.search(stem)
    if legacy_multi:
        cleaned["noise_tokens"].append(legacy_multi.group("tag"))
        stem = stem[:legacy_multi.start("separator")]
    chinese_episode_tokens = [
        match.group(0) for match in _CHINESE_EPISODE_TOKEN.finditer(stem)
    ]
    cleaned["noise_tokens"] = _unique_text((
        *cleaned["noise_tokens"],
        *(match.group(0) for match in _NOISE.finditer(stem)),
        *chinese_episode_tokens,
    ))
    stem = _CHINESE_EPISODE_TOKEN.sub(" ", stem)
    stem = _NOISE.sub(" ", stem)
    # 纯数字正式片名（2012 / 1917 / 1984 / 2001 / 2046）不能被当作
    # 发布年份整体删除。先观察剥离技术标签后的剩余文本；只有它不再是
    # 单一四位数字片名时，才清理普通发布年份。
    numeric_title_probe = re.sub(r"[\[\]【】(){}]", " ", stem)
    numeric_title_probe = re.sub(r"[._+\-·]+", " ", numeric_title_probe)
    numeric_title_probe = re.sub(r"\s+", " ", numeric_title_probe).strip()
    if _YEAR_TOKEN.fullmatch(numeric_title_probe):
        stem = numeric_title_probe
    else:
        stem = _YEAR_TOKEN.sub(" ", stem)
    stem = re.sub(r"[\[\]【】(){}]", " ", stem)
    stem = re.sub(r"[._+\-·]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem, cleaned


def _prefer_guessit_numeric_title(
    guessed_title: str, cleaned_title: str, guessed_year: object,
) -> bool:
    """GuessIt 保留了正式数字片名时，避免清洗器把它当发布年份删掉。"""
    guessed_text = re.sub(r"\s+", " ", str(guessed_title or "")).strip(" ._-")
    if not guessed_text:
        return False
    numeric_tokens = [match.group(1) for match in _YEAR_TOKEN.finditer(guessed_text)]
    if not numeric_tokens:
        return False
    cleaned_key = _comparison_key(cleaned_title)
    if all(token in cleaned_key.split() for token in numeric_tokens):
        return False
    if _YEAR_TOKEN.fullmatch(guessed_text):
        return True
    release_year = str(guessed_year or "").strip()
    return any(token != release_year for token in numeric_tokens)


def _folder_context(
    parent_path: str, *, episode_context: bool = False,
) -> tuple[str, str, str, int | None]:
    parts = [part.strip() for part in re.split(r"[\\/]+", str(parent_path or "")) if part.strip()]
    media_type = ""
    lowered = "/".join(parts).lower()
    if re.search(r"(?:^|/)(?:tv|shows?|series|剧集|电视剧|动漫)(?:/|$)", lowered):
        media_type = "tv"
    elif re.search(r"(?:^|/)(?:movies?|films?|电影|影片)(?:/|$)", lowered):
        media_type = "movie"
    season = next((
        parsed_season
        for part in reversed(parts)
        if (parsed_season := _extract_season(part, episode_context=episode_context)) is not None
    ), None)
    title = ""
    year = ""
    for part in reversed(parts):
        # 部分调用方会把当前媒体文件的完整路径作为 parent_path 传入。
        # 先移除已知文件扩展名，否则扩展名会阻止尾部 CRC/校验和清理，
        # 进而把 ``[D73B045B].mkv`` 错当成标题的一部分。
        parse_part = strip_media_file_suffix(part).strip()
        if not parse_part:
            continue
        if (
            _GENERIC_FOLDER.fullmatch(parse_part)
            or _CHINESE_SEASON_TOKEN.fullmatch(parse_part)
            or is_special_directory_name(parse_part)
        ):
            continue
        identity_part = _strip_trailing_checksum(parse_part)
        info = _guessit_info(identity_part)
        explicit_episode = _extract_episode(parse_part)
        guessed_episode = (
            explicit_episode
            if explicit_episode is not None
            else _position_number(info.get("episode"))
        )
        guessed_title, _ = _clean_release_stem(str(info.get("title") or ""))
        if not guessed_title:
            first_group = _RELEASE_PREFIX.match(parse_part)
            group_text = first_group.group(1)[1:-1].strip() if first_group else ""
            if group_text and (
                _SEASON_TOKEN.search(group_text)
                or len(re.findall(r"[A-Za-z0-9\u4e00-\u9fff]+", group_text)) >= 3
            ):
                guessed_title = group_text
        match = _YEAR_TOKEN.search(identity_part)
        implicit_folder_season, implicit_folder_span = _implicit_season_hint(
            parse_part,
            episode_context=episode_context or guessed_episode is not None,
        )
        raw_source = _strip_known_episode_suffix(
            parse_part,
            guessed_episode,
            _extract_season(
                parse_part,
                episode_context=episode_context or guessed_episode is not None,
            ),
        )
        raw_source = _remove_text_span(raw_source, implicit_folder_span)
        release_candidates, _ = _non_destructive_release_title_candidates(raw_source)
        raw_candidate, _ = _clean_release_stem(raw_source)
        raw_candidate = _strip_season_tokens(raw_candidate)
        raw_candidate = re.sub(r"\b\d{1,3}\s*[-~]\s*\d{1,3}\b", " ", raw_candidate)
        raw_candidate = re.sub(r"\s+", " ", raw_candidate).strip(" -")
        if release_candidates:
            # 没有季集上下文的整包/Extras 根目录中，发布组投影可能把
            # ``Vol1`` 一并保留下来；GuessIt 能稳定去掉卷号但保留作品的
            # 续季数字。存在隐式季号且有具体集号时则反过来使用投影，
            # 以清除 ``Grand Blue Dreaming 3 - 6`` 中的结构数字。
            candidate = (
                release_candidates[0]
                if implicit_folder_season is not None
                else (guessed_title or release_candidates[0])
            )
        else:
            # GuessIt 的标题在卷号/续季目录中会保留有意义的 ``2``，而
            # raw_candidate 已为了季号推断移除该片段。无发布组投影时优先
            # 使用 GuessIt，避免把第二季根目录退化成第一季同名锚点。
            candidate = guessed_title or raw_candidate
        # 含中文的原始目录标题比 GuessIt 拆出的局部片段更可靠。
        # 例如“不要欺负我，长瀞同学 2nd Attack”不能退化成“Attack”。
        if raw_candidate and re.search(r"[\u4e00-\u9fff]", raw_candidate):
            candidate = raw_candidate
        candidate = _strip_season_tokens(candidate)
        candidate = re.sub(r"\s+", " ", candidate).strip(" -")
        if candidate:
            title = candidate
            year = str(info.get("year") or (match.group(1) if match else ""))
            break
    return title, year, media_type, season


def extract_recognition_context(filename: str, parent_path: str = "") -> RecognitionContext:
    """阶段 1/2：归一化文件名并提取文件/目录上下文。"""
    raw_name = str(filename or "")
    raw_parent_path = str(parent_path or "")
    parse_name = _strip_explicit_tmdb_markers(raw_name)
    parse_parent_path = _strip_explicit_tmdb_markers_from_path(raw_parent_path)
    stem = strip_media_file_suffix(parse_name)
    identity_stem = _strip_trailing_checksum(stem)
    guessed = _guessit_info(identity_stem)
    guessed_title = str(guessed.get("title") or "").strip()
    guessed_year = _position_number(guessed.get("year"))
    year_tokens = [match.group(1) for match in _YEAR_TOKEN.finditer(identity_stem)]
    filename_year = str(guessed_year or (year_tokens[-1] if year_tokens else ""))
    explicit_episode = _extract_episode(stem)
    guessed_episode = _position_number(guessed.get("episode"))
    guessed_season = _season_number(guessed.get("season"))
    has_season_range = bool(_SEASON_RANGE_TOKEN.search(stem))
    if has_season_range:
        guessed_season = None
    untrusted_guessed_episode = _guessit_episode_is_untrusted(
        stem,
        explicit_episode,
        guessed_episode,
        guessed_season,
    )
    if untrusted_guessed_episode:
        guessed_episode = None
        guessed_season = None
    if _has_unaccepted_release_x_position(stem):
        # GuessIt 会把 ``16x9`` 当成 S16E09；画面比例不能改变媒体类型。
        guessed_episode = None
        guessed_season = None
    episode = explicit_episode if explicit_episode is not None else guessed_episode
    explicit_season = _extract_explicit_season(
        stem, episode_context=episode is not None,
    )
    implicit_season, implicit_season_span = _implicit_season_hint(
        stem, episode_context=episode is not None,
    )
    season = explicit_season if explicit_season is not None else implicit_season
    special_episode = _extract_special_episode(stem)
    # GuessIt 会把 ``20th Remaster - 069`` 中的 ordinal 误报成 Season 0。
    # S00/OVA 等特别篇已经由显式季号与 special_episode 单独识别，这里只
    # 接受正季号，避免普通长篇发布被错误送进 Specials。
    if (
        season is None
        and explicit_episode is None
        and guessed_season is not None
        and guessed_season > 0
    ):
        # 已由 ``Title - 100`` / ``Title [100]`` 这类稳定裸集号规则命中时，
        # GuessIt 可能把三位集号的首位同时误报成季号（100 -> S01E100）。
        # 裸集号应保持 season=None，交给目录级连续集包证据按 TMDB 季容量
        # 换算；只有本地规则没解析到集号时才采纳 GuessIt 的季号。
        season = guessed_season
    if special_episode is not None:
        season = 0
        episode = special_episode
    title_source = _strip_known_episode_suffix(stem, episode, season)
    if explicit_season is None and implicit_season is not None:
        title_source = _remove_text_span(title_source, implicit_season_span)
    if special_episode is not None:
        title_source = _SPECIAL_EPISODE_TOKEN.sub(" ", title_source)
        title_source = strip_special_media_markers(title_source)
    release_title_candidates, semantic_components = (
        _non_destructive_release_title_candidates(title_source)
    )
    filename_title, cleaned = _clean_release_stem(title_source)
    for component_name, values in semantic_components.items():
        cleaned[component_name] = _unique_text((
            *cleaned.get(component_name, []), *values,
        ))
    filename_title = re.sub(r"\s+", " ", _strip_season_tokens(filename_title)).strip(" ._-")
    guessed_title = re.sub(r"\s+", " ", _strip_season_tokens(guessed_title)).strip(" ._-")
    if not filename_title and guessed_title:
        filename_title = guessed_title
    elif _prefer_guessit_numeric_title(guessed_title, filename_title, guessed_year):
        filename_title = guessed_title
    folder_title, folder_year, folder_type, folder_season = _folder_context(
        parse_parent_path, episode_context=episode is not None,
    )
    if season is None:
        season = folder_season
    filename_is_generic = not filename_title or bool(re.fullmatch(r"(?i)(?:e|ep|episode)?\s*\d+", filename_title))
    if filename_is_generic and guessed_title:
        filename_title = guessed_title
        filename_is_generic = False
    if release_title_candidates:
        filename_title = release_title_candidates[0]
        filename_is_generic = False
    primary_edition = _PRIMARY_TRAILING_RELEASE_EDITION_NOISE.search(filename_title)
    if primary_edition:
        shortened_title = _PRIMARY_TRAILING_RELEASE_EDITION_NOISE.sub(
            "", filename_title
        ).strip(" ._-—–:：")
        if shortened_title and not _low_information_query(shortened_title):
            cleaned["noise_tokens"] = _unique_text((
                *cleaned.get("noise_tokens", []), primary_edition.group(0),
            ))
            filename_title = shortened_title
            filename_is_generic = False
    normalized_title = folder_title if filename_is_generic and folder_title else filename_title
    guessed_type = str(guessed.get("type") or "").lower()
    media_type = "tv" if (
        season is not None
        or episode is not None
        or (guessed_type == "episode" and not untrusted_guessed_episode)
    ) else (folder_type or "movie")
    original_title, _ = _clean_release_stem(
        _strip_known_episode_suffix(stem, episode, season)
    )
    original_title = re.sub(
        r"\s+", " ", _strip_season_tokens(original_title)
    ).strip(" ._-")
    variants = _unique_text(
        release_title_candidates
        + ([original_title] if original_title and original_title != filename_title else [])
        + _split_title_variants(normalized_title)
        + _split_title_variants(folder_title)
    )
    return RecognitionContext(
        filename=raw_name,
        parent_path=raw_parent_path,
        normalized_title=normalized_title,
        filename_title=filename_title,
        filename_year=filename_year,
        folder_title=folder_title,
        folder_year=folder_year,
        media_type=media_type,
        season=season,
        episode=episode,
        title_variants=variants,
        cleaned_components=cleaned,
    )


def _explicit_animation_source_marker(context: RecognitionContext) -> str:
    """返回发布源明确声明的动画证据；普通标题和类型猜测不参与。"""
    values = (
        str(context.filename or ""),
        str(context.parent_path or ""),
        *(
            str(item or "")
            for items in (context.cleaned_components or {}).values()
            for item in (items or ())
        ),
    )
    for value in values:
        match = _EXPLICIT_DONGHUA_MARKER.search(value)
        if match:
            return match.group(0)
    return ""


def _tmdb_genre_ids(candidate: dict) -> set[int]:
    """兼容 TMDB 搜索 ``genre_ids`` 与详情 ``genres`` 两种结构。"""
    values: list[object] = list(candidate.get("genre_ids") or ())
    for genre in candidate.get("genres") or ():
        values.append(genre.get("id") if isinstance(genre, dict) else genre)
    genre_ids: set[int] = set()
    for value in values:
        try:
            genre_ids.add(int(value))
        except (TypeError, ValueError):
            continue
    return genre_ids


def _enclosed_high_season_fallback_context(
    context: RecognitionContext,
) -> RecognitionContext | None:
    """为相邻方括号高季号生成须经 TMDB 位置验证的第二解释。"""
    if (
        context.media_type != "tv"
        or context.season is not None
        or context.episode is None
        or context.episode < 1
    ):
        return None
    raw = _strip_explicit_tmdb_markers(str(context.filename or ""))
    stem = raw.rsplit(".", 1)[0] if "." in raw else raw
    for match in reversed(list(
        _AMBIGUOUS_ENCLOSED_HIGH_SEASON_EPISODE_TOKEN.finditer(stem)
    )):
        season = int(match.group("season"))
        episode = int(match.group("episode"))
        if episode != context.episode:
            continue
        tail = stem[match.end():]
        # 单独的语言/版本标签不足以证明这是发布方的季号语法；至少要求
        # 一个可识别发布组前缀和编码/分辨率/来源等技术证据。
        prefix = stem[:match.start()].strip()
        release_prefix = _RELEASE_PREFIX.fullmatch(prefix)
        if not release_prefix:
            continue
        prefix_content = _bracket_content(release_prefix.group(1))
        title_remainder = stem[match.start():]
        if not (
            _is_release_prefix(prefix_content, title_remainder)
            or _probable_unknown_release_prefix(prefix_content, title_remainder)
        ):
            continue
        if not _IMPLICIT_SEASON_TECHNICAL_EVIDENCE.search(tail):
            continue
        title, title_cleaned = _clean_release_stem(match.group("title"))
        title = re.sub(r"\s+", " ", title).strip(" ._-—–:：")
        if not title or _low_information_query(title):
            continue
        cleaned = {
            key: list(values) for key, values in context.cleaned_components.items()
        }
        cleaned["ambiguous_enclosed_season"] = [
            f"title={title};season={season};episode={episode}"
        ]
        for key, values in title_cleaned.items():
            cleaned[key] = _unique_text((*cleaned.get(key, []), *values))
        return replace(
            context,
            normalized_title=title,
            filename_title=title,
            media_type="tv",
            season=season,
            episode=episode,
            title_variants=_unique_text((title, *_split_title_variants(title))),
            cleaned_components=cleaned,
        )
    return None


_SOURCE_LOW_INFORMATION_FOLDERS_KEY = "source_low_information_folder_titles"
_SOURCE_EXPLICIT_ANIMATION_MARKERS_KEY = "source_explicit_animation_markers"


def _source_low_information_folder_titles(
    context: RecognitionContext,
) -> list[str]:
    filename_queries = _split_title_variants(context.filename_title)
    if not any(not _low_information_query(query) for query in filename_queries):
        return []
    return [
        query for query in _split_title_variants(context.folder_title)
        if _low_information_query(query)
    ]


def _inherit_source_query_provenance(
    context: RecognitionContext, source_context: RecognitionContext | None,
) -> None:
    """保留预处理前会影响安全决策的来源证据。"""
    if source_context is None:
        return
    cleaned = {
        key: list(values) for key, values in context.cleaned_components.items()
    }
    weak_folders = list(
        cleaned.get(_SOURCE_LOW_INFORMATION_FOLDERS_KEY, [])
    )
    weak_folders.extend(_source_low_information_folder_titles(source_context))
    if weak_folders:
        cleaned[_SOURCE_LOW_INFORMATION_FOLDERS_KEY] = _unique_text(weak_folders)
    animation_marker = _explicit_animation_source_marker(source_context)
    if animation_marker:
        cleaned[_SOURCE_EXPLICIT_ANIMATION_MARKERS_KEY] = _unique_text((
            *cleaned.get(_SOURCE_EXPLICIT_ANIMATION_MARKERS_KEY, []),
            animation_marker,
        ))
    context.cleaned_components = cleaned


def _weak_folder_title_keys(context: RecognitionContext) -> set[str]:
    """返回不能覆盖可靠文件标题的低信息目录标题键。"""
    folder_titles = list(
        context.cleaned_components.get(_SOURCE_LOW_INFORMATION_FOLDERS_KEY, [])
    )
    folder_titles.extend(_source_low_information_folder_titles(context))
    return {
        _comparison_key(query) for query in folder_titles if query
    }


def generate_query_variants(context: RecognitionContext) -> list[str]:
    """阶段 3：按文件标题、双标题拆分和父目录标题生成有序查询。

    ``1`` / ``Q`` 这类目录名可以在文件名本身也缺少有效标题时用于候选召回，
    但不能在文件名已有完整标题时重新进入搜索并以精确命中覆盖文件证据。
    """
    weak_folder_keys = _weak_folder_title_keys(context)
    folder_queries = [
        query for query in _split_title_variants(context.folder_title)
        if _comparison_key(query) not in weak_folder_keys
    ]
    extra_variants = [
        query for query in context.title_variants
        if _comparison_key(query) not in weak_folder_keys
    ]
    return _unique_text(
        _split_title_variants(context.normalized_title)
        + _split_title_variants(context.filename_title)
        + folder_queries
        + extra_variants
    )[:8]


_SEARCH_ONLY_TRAILING_DUB_NOISE = re.compile(
    r"(?ix)(?:[ ._\-—–:：]+|^)"
    r"(?:中文|国语|国配|粤语|粤配|台配|港配|普通话)"
    r"(?:[ ._\-]*(?:配音|音轨))?$"
    r"|(?:[ ._\-—–:：]+|^)(?:dual[ ._\-]*audio|dubbed?|dub)$"
)
_SEARCH_ONLY_TRAILING_EDITION_NOISE = re.compile(
    r"(?ix)(?:[ ._\-—–:：]+|^)"
    r"(?:(?:\d{1,3}(?:st|nd|rd|th)[ ._\-]*)?remaster(?:ed)?|"
    r"(?:4k|uhd)[ ._\-]*remaster(?:ed)?)$"
)


def generate_search_only_query_variants(
    context: RecognitionContext,
    primary_queries: list[str] | None = None,
) -> list[str]:
    """生成只用于补召回、绝不能直接支撑自动整理的保守降噪查询。

    这些变体仅在正常标题（含有年/无年份两轮）完全没有候选时使用；它们
    不会进入 ``score_candidate`` 的标题锚点，因此不能把删词后的短标题伪装
    成高置信度识别。命中后仍强制人工确认，但可避免用户面对空候选列表。
    """
    sources = list(primary_queries or generate_query_variants(context))
    variants: list[str] = []
    for source in sources:
        value = str(source or "").strip()
        if not value:
            continue
        cleaned = _SEARCH_ONLY_TRAILING_DUB_NOISE.sub("", value).strip(" ._-—–:：")
        cleaned = _SEARCH_ONLY_TRAILING_EDITION_NOISE.sub("", cleaned).strip(" ._-—–:：")
        if cleaned and _comparison_key(cleaned) != _comparison_key(value):
            variants.append(cleaned)
    primary_keys = {_comparison_key(item) for item in sources if item}
    return [
        item for item in _unique_text(variants)
        if _comparison_key(item) not in primary_keys and not _low_information_query(item)
    ][:4]


def _candidate_aliases(candidate: dict) -> list[str]:
    values: list[object] = []

    def append_collection(collection: object) -> None:
        if isinstance(collection, dict):
            collection = (
                collection.get("titles")
                or collection.get("results")
                or collection.get("translations")
                or []
            )
        for item in collection if isinstance(collection, list) else []:
            if isinstance(item, dict):
                data = item.get("data") if isinstance(item.get("data"), dict) else {}
                values.extend((
                    item.get("title"), item.get("name"),
                    data.get("title"), data.get("name"), data.get("english_name"),
                ))
            else:
                values.append(item)

    append_collection(candidate.get("aliases") or [])
    append_collection(candidate.get("alternative_titles") or [])
    append_collection(candidate.get("translations") or [])
    return _unique_text(values)


_GENERIC_TMDB_SEASON_NAME = re.compile(
    r"(?ix)^\s*(?:"
    r"s(?:eason)?[ ._-]*\d{1,2}|"
    r"第\s*[零〇一二两三四五六七八九十\d]{1,3}\s*季|"
    r"(?:specials?|特别篇|特別篇|番外篇)"
    r")\s*$"
)


def _season_name_is_significant(value: str) -> bool:
    """TMDB 季名必须包含可区分篇章的真实语义，不能只写“第 2 季”。"""
    text = str(value or "").strip()
    if not text or _GENERIC_TMDB_SEASON_NAME.fullmatch(text):
        return False
    cjk_count = len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff]", text))
    latin_tokens = re.findall(r"[A-Za-z][A-Za-z0-9']*", text)
    return cjk_count >= 4 or (
        len(latin_tokens) >= 2
        and len("".join(latin_tokens)) >= 8
    )


def infer_tmdb_season_from_title_evidence(
    filename: str,
    parent_path: str,
    detail: dict | None,
    *,
    episode: int | None = None,
) -> int | None:
    """用发布名中的篇章名保守映射唯一 TMDB 季。

    这里只消费 TMDB 已返回的季名称，并要求名称在文件/最近媒体目录中完整
    出现；繁简体通过受限拼音键辅助。它不会把“第二季”或任意副标题直接换算
    成季号，也不会在多个季名同时命中时猜测。
    """
    if not isinstance(detail, dict):
        return None
    try:
        source_episode = int(episode) if episode is not None else None
    except (TypeError, ValueError):
        source_episode = None

    context = extract_recognition_context(filename, parent_path)
    nearest_parent = next((
        part.strip()
        for part in reversed(re.split(r"[\\/]+", str(parent_path or "")))
        if part.strip() and not _GENERIC_FOLDER.fullmatch(part.strip())
    ), "")
    source_values = _unique_text((
        context.normalized_title,
        context.filename_title,
        context.folder_title,
        *context.title_variants,
        nearest_parent,
    ))
    def compact_evidence_key(value: str, *, pinyin: bool = False) -> str:
        raw = _han_pinyin_key(value) if pinyin else _comparison_key(value)
        return re.sub(r"[^a-z0-9\u3040-\u30ff\u3400-\u9fff]+", "", raw.casefold())

    source_keys = [
        compact_evidence_key(value)
        for value in source_values if value
    ]
    source_pinyin = [
        compact_evidence_key(value, pinyin=True)
        for value in source_values if value
    ]

    matched: list[int] = []
    for item in detail.get("seasons") or []:
        if not isinstance(item, dict):
            continue
        season = _strict_non_negative_int(item.get("season_number"))
        count = _strict_non_negative_int(item.get("episode_count"))
        name = str(item.get("name") or "").strip()
        if (
            season is None or season <= 1
            or not _season_name_is_significant(name)
            or (
                source_episode is not None
                and count is not None
                and count > 0
                and source_episode > count
            )
        ):
            continue
        name_key = compact_evidence_key(name)
        name_pinyin = compact_evidence_key(name, pinyin=True)
        direct_match = bool(
            name_key and len(name_key) >= 4
            and any(name_key in source for source in source_keys)
        )
        pinyin_match = bool(
            name_pinyin and len(name_pinyin) >= 8
            and any(name_pinyin in source for source in source_pinyin)
        )
        if direct_match or pinyin_match:
            matched.append(season)
    unique = sorted(set(matched))
    return unique[0] if len(unique) == 1 else None


def has_unresolved_candidate_title_remainder(
    context: RecognitionContext,
    match: MatchResult | None,
    detail: dict | None = None,
) -> bool:
    """判断已命中系列基础名后，发布标题是否仍残留未解释的篇章语义。"""
    primary_queries = generate_query_variants(context)
    if not primary_queries:
        return False
    candidate_values: list[str] = []
    if match is not None:
        candidate_values.append(str(match.title or ""))
        for candidate in list(match.candidates or []):
            candidate_values.extend((candidate.title, candidate.original_title))
            candidate_values.extend(candidate.aliases or [])
    if isinstance(detail, dict):
        candidate_values.extend((
            str(detail.get("name") or detail.get("title") or ""),
            str(detail.get("original_name") or detail.get("original_title") or ""),
        ))
        candidate_values.extend(_candidate_aliases(detail))
    candidates = _unique_text(candidate_values)
    if not candidates:
        return False
    # 完整标题或别名已经强命中时，不能把作品自身副标题误报为未决季名。
    if max(
        (
            _title_similarity_score(primary, candidate)
            for primary in primary_queries
            for candidate in candidates
        ),
        default=0.0,
    ) >= 0.97:
        return False
    return any(
        _has_distinctive_title_remainder(primary_queries, candidate)
        for candidate in candidates
    )


def implicit_season_conflicts_with_candidate_title(
    filename: str,
    parent_path: str,
    match: MatchResult | None,
    detail: dict | None = None,
) -> bool:
    """判断文件名或父目录中的隐式季号是否其实属于作品正式标题。

    ``Title II [05] [1080p]`` 既可能表示第二季第 5 集，也可能是作品名
    ``Title II`` 的第 5 集。相同歧义也可能由父目录提供，例如目录
    ``Title II [01]`` 下的文件 ``05.mkv``。纯发布名解析无法安全区分，
    因此在候选确定后再做一次候选相对校验：若保留罗马数字的完整标题与
    候选主标题强匹配，而移除该数字后的基础标题不匹配，就不能把它消费为
    季号。

    显式 ``S02``、``Season 2``、``第二季`` 不参与此保护；它们本身就是
    确定性的季号证据。
    """
    raw = str(filename or "")
    stem = raw.rsplit(".", 1)[0] if "." in raw else raw
    context = extract_recognition_context(filename, parent_path)
    episode = _extract_episode(stem) or context.episode
    if episode is None:
        return False
    if _extract_explicit_season(stem, episode_context=True) is not None:
        return False

    # 与 _folder_context 一致，从离文件最近的父目录向外查找。这里只记录
    # 真正产生隐式季号的片段，避免拿整个绝对路径参与标题相似度比较。
    sources = [stem]
    sources.extend(
        part.strip()
        for part in reversed(re.split(r"[\\/]+", str(parent_path or "")))
        if part.strip()
    )
    implicit_source = ""
    implicit_span: tuple[int, int] | None = None
    for source in sources:
        parse_source = strip_media_file_suffix(source).strip()
        if not parse_source:
            continue
        if _extract_explicit_season(parse_source, episode_context=True) is not None:
            # 最近层级已经给出显式季号时，它优先于更外层目录中的罗马数字；
            # 继续向外搜索会把库名/作品名中的数字错误消费为季号。
            return False
        implicit_season, span = _implicit_season_hint(
            parse_source,
            episode_context=True,
        )
        if implicit_season is not None and span is not None:
            implicit_source = parse_source
            implicit_span = span
            break
    if not implicit_source or implicit_span is None:
        return False

    full_context = extract_recognition_context(implicit_source)
    base_source = _remove_text_span(implicit_source, implicit_span)
    base_context = extract_recognition_context(base_source)
    full_title_variants = _unique_text((
        full_context.normalized_title,
        full_context.filename_title,
        *full_context.title_variants,
    ))
    base_title_variants = _unique_text((
        base_context.normalized_title,
        base_context.filename_title,
        *base_context.title_variants,
    ))
    if not full_title_variants or not base_title_variants:
        return False

    # 这里只能使用“系列主标题”判断作品名冲突。候选别名和 translations
    # 可能包含第二季/篇章标题（例如 ``... II``）；若把它们也当成系列
    # 正式标题，会把真实的第二季误拦成“作品名中的罗马数字”。
    candidate_values: list[str] = []
    if match is not None:
        candidate_values.append(str(match.title or ""))
    if isinstance(detail, dict):
        candidate_values.extend((
            str(detail.get("name") or detail.get("title") or ""),
            str(detail.get("original_name") or detail.get("original_title") or ""),
        ))
    candidates = _unique_text(candidate_values)
    if not candidates:
        return False

    # 同一个候选必须“强匹配完整罗马数字标题”，且不能同样强匹配去掉
    # 罗马数字后的系列基础名。后者可避免本地化标题或轻微标点差异造成误判。
    return any(
        _title_similarity_score(source, candidate) >= 0.97
        and max(
            (
                _title_similarity_score(base, candidate)
                for base in base_title_variants
            ),
            default=0.0,
        ) < 0.97
        for source in full_title_variants
        for candidate in candidates
    )


def _eligible_latin_alias_query(value: str) -> bool:
    """仅把信息充分的拉丁字母标题用于 Romaji 精确别名校验。"""
    key = _comparison_key(value)
    compact = key.replace(" ", "")
    return bool(
        key
        and len(compact) >= 4
        and re.fullmatch(r"[a-z0-9 ]+", key)
        and not _low_information_query(value)
    )


def _candidate_identity_labels(candidate: dict) -> list[str]:
    """返回搜索主标题与详情别名组成的候选身份标签。"""
    return _unique_text((
        candidate.get("name"),
        candidate.get("title"),
        candidate.get("original_name"),
        candidate.get("original_title"),
        *_candidate_aliases(candidate),
    ))


def _ambiguous_exact_alias_queries(
    queries: list[str],
    scored: list[tuple[CandidateScoreBreakdown, dict]],
    threshold: float,
) -> list[str]:
    """返回同时精确命中多个 TMDB ID 的 Romaji/拉丁别名。"""
    eligible = {
        _comparison_key(query): query
        for query in queries
        if _eligible_latin_alias_query(query)
    }
    if not eligible:
        return []
    matched_ids: dict[str, set[str]] = {key: set() for key in eligible}
    for breakdown, candidate in scored:
        if breakdown.rejected_constraints or breakdown.final_score < threshold:
            continue
        candidate_id = str(candidate.get("id") or "").strip()
        if not candidate_id:
            continue
        alias_keys = {_comparison_key(alias) for alias in _candidate_aliases(candidate)}
        for query_key in eligible:
            if query_key in alias_keys:
                matched_ids[query_key].add(candidate_id)
    return [eligible[key] for key, ids in matched_ids.items() if len(ids) >= 2]


def _ambiguous_exact_identity_queries(
    queries: list[str],
    scored: list[tuple[CandidateScoreBreakdown, dict]],
    threshold: float,
) -> list[str]:
    """返回同时精确命中多个候选主标题/别名的高信息查询。

    TMDB 搜索响应本身就可能返回多个同名中文作品。此前位置消歧只覆盖
    Romaji 详情别名，导致“大主宰.S02E33”一类输入在错误的同名短剧上
    提前失败。这里仅收集精确同名且达到阈值的候选，后续仍必须通过
    TMDB 季集范围逐一硬排除，任何证据缺失都会失败关闭。
    """
    eligible = {
        _comparison_key(query): query
        for query in queries
        if query and not _low_information_query(query)
    }
    if not eligible:
        return []
    matched_ids: dict[str, set[str]] = {key: set() for key in eligible}
    for breakdown, candidate in scored:
        if breakdown.rejected_constraints or breakdown.final_score < threshold:
            continue
        candidate_id = str(candidate.get("id") or "").strip()
        if not candidate_id:
            continue
        identity_keys = {
            _comparison_key(label) for label in _candidate_identity_labels(candidate)
            if label
        }
        for query_key in eligible:
            if query_key in identity_keys:
                matched_ids[query_key].add(candidate_id)
    return [eligible[key] for key, ids in matched_ids.items() if len(ids) >= 2]


_TARGET_SEASON_YEAR_EVIDENCE_KIND = "tmdb_tv_target_season_air_year"

# 季度线索只是弱证据：它能在标题与年份相当的候选之间提升正确项排序，
# 但绝不能单独绑定身份，也不得绕过 TMDB ID、季集范围与别名冲突安全门。
_QUARTER_BONUS = 0.03
_QUARTER_WORD_CLUES = {
    "冬季番": "Q1", "冬番": "Q1", "一月番": "Q1", "1月番": "Q1",
    "春季番": "Q2", "春番": "Q2", "四月番": "Q2", "4月番": "Q2",
    "夏季番": "Q3", "夏番": "Q3", "七月番": "Q3", "7月番": "Q3",
    "秋季番": "Q4", "秋番": "Q4", "十月番": "Q4", "10月番": "Q4",
}
_QUARTER_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])[Qq]([1-4])(?![0-9])")


def parse_release_quarter(*texts: object) -> str:
    """解析 Q1–Q4 与春夏秋冬番线索；线索缺失或互相冲突时返回空串。"""
    found: set[str] = set()
    for item in texts:
        text = str(item or "")
        if not text:
            continue
        for match in _QUARTER_TOKEN_RE.finditer(text):
            found.add(f"Q{match.group(1)}")
        for word, quarter in _QUARTER_WORD_CLUES.items():
            if word in text:
                found.add(quarter)
    return next(iter(found)) if len(found) == 1 else ""


def _air_date_quarter(air_date: object) -> str:
    """把 TMDB 播出日期换算成季度；日期不完整时返回空串。"""
    match = re.fullmatch(
        r"((?:19|20)\d{2})-(0[1-9]|1[0-2])-(\d{2})", str(air_date or "").strip()
    )
    if not match:
        return ""
    return f"Q{(int(match.group(2)) - 1) // 3 + 1}"


def _validated_target_season_year_evidence(
    candidate: dict,
    context: RecognitionContext,
    expected_year: str,
) -> dict[str, object] | None:
    """复核候选上已经由 TMDB 详情构造的“目标季首播年”强证据。"""
    evidence = candidate.get("_verified_target_season_year")
    if not isinstance(evidence, dict):
        return None
    if str(evidence.get("kind") or "") != _TARGET_SEASON_YEAR_EVIDENCE_KIND:
        return None
    if str(candidate.get("media_type") or "").strip().lower() != "tv":
        return None
    if str(evidence.get("media_type") or "").strip().lower() != "tv":
        return None
    candidate_id = str(candidate.get("id") or "").strip()
    if not candidate_id or str(evidence.get("tmdb_id") or "").strip() != candidate_id:
        return None
    expected = str(expected_year or "").strip()
    if not re.fullmatch(r"(?:19|20)\d{2}", expected):
        return None
    if str(evidence.get("expected_year") or "").strip() != expected:
        return None
    if context.media_type != "tv" or context.season is None or context.episode is None:
        return None
    try:
        source_season = int(evidence.get("source_season"))
        source_episode = int(evidence.get("source_episode"))
        target_season = int(evidence.get("target_season"))
        target_episode = int(evidence.get("target_episode"))
    except (TypeError, ValueError):
        return None
    if (
        source_season != context.season
        or source_episode != context.episode
        or target_season != context.season
        or target_episode != context.episode
        or target_season < 1
        or target_episode < 1
    ):
        return None
    season_air_date = str(evidence.get("season_air_date") or "").strip()
    if season_air_date[:4] != expected:
        return None
    validation = evidence.get("position_validation")
    if not isinstance(validation, dict):
        return None
    if not bool(validation.get("required")) or not bool(validation.get("passed")):
        return None
    if str(validation.get("reason") or "") != "episode_verified":
        return None
    try:
        if int(validation.get("season")) != target_season:
            return None
        if int(validation.get("episode")) != target_episode:
            return None
    except (TypeError, ValueError):
        return None
    mapping = evidence.get("episode_mapping")
    if not isinstance(mapping, dict) or mapping.get("changed") is not False:
        return None
    try:
        if int(mapping.get("source_season")) != source_season:
            return None
        if int(mapping.get("source_episode")) != source_episode:
            return None
        if int(mapping.get("target_season")) != target_season:
            return None
        if int(mapping.get("target_episode")) != target_episode:
            return None
    except (TypeError, ValueError):
        return None
    return dict(evidence)


def _build_target_season_year_evidence(
    *,
    detail: dict,
    tmdb_id: str,
    context: RecognitionContext,
    expected_year: str,
    mapping: EpisodeMappingPlan,
) -> dict[str, object] | None:
    """从 TMDB 剧集详情构造严格绑定源 SxxExx 的目标季年份证据。"""
    expected = str(expected_year or "").strip()
    if (
        context.media_type != "tv"
        or context.season is None
        or context.season < 1
        or context.episode is None
        or context.episode < 1
        or not re.fullmatch(r"(?:19|20)\d{2}", expected)
        or not str(tmdb_id or "").strip()
    ):
        return None
    detail_id = str(detail.get("id") or "").strip() if isinstance(detail, dict) else ""
    if detail_id != str(tmdb_id).strip():
        return None
    if (
        mapping.changed
        or mapping.source_season != context.season
        or mapping.source_episode != context.episode
        or mapping.target_season != context.season
        or mapping.target_episode != context.episode
    ):
        return None
    seasons = detail.get("seasons") if isinstance(detail, dict) else None
    if not isinstance(seasons, list):
        return None
    target = next((
        item for item in seasons
        if isinstance(item, dict)
        and _strict_non_negative_int(item.get("season_number")) == context.season
    ), None)
    if not isinstance(target, dict):
        return None
    season_air_date = str(target.get("air_date") or "").strip()
    if season_air_date[:4] != expected:
        return None
    validation = _validate_tmdb_position(
        detail, "tv", context.season, context.episode,
    )
    if (
        not bool(validation.get("required"))
        or not bool(validation.get("passed"))
        or str(validation.get("reason") or "") != "episode_verified"
    ):
        return None
    return {
        "kind": _TARGET_SEASON_YEAR_EVIDENCE_KIND,
        "tmdb_id": str(tmdb_id).strip(),
        "media_type": "tv",
        "expected_year": expected,
        "source_season": context.season,
        "source_episode": context.episode,
        "target_season": context.season,
        "target_episode": context.episode,
        "season_air_date": season_air_date,
        "position_validation": dict(validation),
        "episode_mapping": mapping.to_dict(),
    }


def _validated_target_season_year_proof_evidence(
    evidence: object,
    *,
    tmdb_id: str,
    expected_year: str,
    source_season: int | None,
    source_episode: int,
    target_season: int,
    target_episode: int,
    proof_position_validation: dict,
    proof_episode_mapping: object,
) -> dict[str, object] | None:
    """复核已经序列化进自动身份 proof 的目标季年份证据。"""
    if not isinstance(evidence, dict):
        return None
    if str(evidence.get("kind") or "") != _TARGET_SEASON_YEAR_EVIDENCE_KIND:
        return None
    if str(evidence.get("media_type") or "").strip().lower() != "tv":
        return None
    if str(evidence.get("tmdb_id") or "").strip() != str(tmdb_id or "").strip():
        return None
    expected = str(expected_year or "").strip()
    if (
        not re.fullmatch(r"(?:19|20)\d{2}", expected)
        or str(evidence.get("expected_year") or "").strip() != expected
        or source_season is None
        or source_season < 1
    ):
        return None
    try:
        evidence_source_season = int(evidence.get("source_season"))
        evidence_source_episode = int(evidence.get("source_episode"))
        evidence_target_season = int(evidence.get("target_season"))
        evidence_target_episode = int(evidence.get("target_episode"))
    except (TypeError, ValueError):
        return None
    if (
        evidence_source_season != source_season
        or evidence_source_episode != source_episode
        or evidence_target_season != target_season
        or evidence_target_episode != target_episode
        or source_season != target_season
        or source_episode != target_episode
    ):
        return None
    if str(evidence.get("season_air_date") or "").strip()[:4] != expected:
        return None

    validation = evidence.get("position_validation")
    if not isinstance(validation, dict):
        return None
    for payload in (validation, proof_position_validation):
        if not bool(payload.get("required")) or not bool(payload.get("passed")):
            return None
        if str(payload.get("reason") or "") != "episode_verified":
            return None
        try:
            if int(payload.get("season")) != target_season:
                return None
            if int(payload.get("episode")) != target_episode:
                return None
        except (TypeError, ValueError):
            return None

    for mapping in (evidence.get("episode_mapping"), proof_episode_mapping):
        if not isinstance(mapping, dict) or mapping.get("changed") is not False:
            return None
        try:
            if int(mapping.get("source_season")) != source_season:
                return None
            if int(mapping.get("source_episode")) != source_episode:
                return None
            if int(mapping.get("target_season")) != target_season:
                return None
            if int(mapping.get("target_episode")) != target_episode:
                return None
        except (TypeError, ValueError):
            return None
    return dict(evidence)


def score_candidate(context: RecognitionContext, candidate: dict) -> CandidateScoreBreakdown:
    """阶段 4：独立计算标题、原名/别名、年份和媒体类型约束。"""
    queries = generate_query_variants(context) or [context.normalized_title]
    primary_queries = _unique_text([
        context.normalized_title, context.filename_title, context.folder_title,
    ]) or queries[:1]
    primary_lengths = {
        query: len(_comparison_key(query).replace(" ", ""))
        for query in primary_queries
    }
    max_primary_length = max(primary_lengths.values(), default=0)
    anchor_queries = [
        query for query in primary_queries
        if primary_lengths[query] >= max(1, int(max_primary_length * 0.8))
    ] or primary_queries[:1]
    anchor_keys = {_comparison_key(query) for query in anchor_queries}
    title = str(candidate.get("name") or candidate.get("title") or "")
    original = str(candidate.get("original_name") or candidate.get("original_title") or "")
    aliases = _candidate_aliases(candidate)

    def best_score(values: list[str], query_pool: list[str]) -> tuple[float, str, str]:
        scored = [
            (_title_similarity_score(query, value), value, query)
            for query in query_pool
            for value in values
            if value
        ]
        return max(
            scored,
            default=(0.0, "", ""),
            key=lambda item: (item[0], _comparison_key(item[2]) in anchor_keys),
        )

    title_score, title_match, title_query = best_score([title], queries)
    original_score, original_match, original_query = best_score([original], queries)
    alias_score, alias_match, alias_query = best_score(aliases, queries)
    best_title_score, matched_title, matched_query = max(
        (title_score, title_match, title_query),
        (original_score, original_match, original_query),
        (alias_score, alias_match, alias_query),
        key=lambda item: (item[0], _comparison_key(item[2]) in anchor_keys),
    )
    primary_title_score = max(
        best_score([title], anchor_queries)[0],
        best_score([original], anchor_queries)[0],
        best_score(aliases, anchor_queries)[0],
    )

    expected_year = context.filename_year or context.folder_year
    candidate_date = str(candidate.get("first_air_date") or candidate.get("release_date") or "")
    candidate_year = candidate_date[:4]
    season_year_evidence = _validated_target_season_year_evidence(
        candidate, context, expected_year,
    )
    year_score = 0.0
    year_penalty = 0.0
    if season_year_evidence is not None:
        # 文件年份明确对应当前目标季，而不是系列首播年。该证据已经绑定
        # TMDB ID、identity 季集映射、目标季 air_date 与 episode_count，
        # 因而在评分语义上等价于普通年份精确命中；但不会降低全局阈值。
        year_score = 1.0
    elif expected_year and candidate_year:
        delta = abs(int(expected_year) - int(candidate_year))
        if delta == 0:
            year_score = 1.0
        elif delta == 1:
            year_score = 0.5
            year_penalty = -0.08
        else:
            year_penalty = -0.30

    rejected: list[str] = []
    candidate_type = str(candidate.get("media_type") or "")
    media_type_score = 1.0
    constraint_penalty = 0.0
    if candidate_type in {"movie", "tv"} and candidate_type != context.media_type:
        rejected.append("media_type_mismatch")
        media_type_score = 0.0
        constraint_penalty = -1.0

    high_information_anchors = [
        query for query in anchor_queries if not _low_information_query(query)
    ]
    weak_folder_keys = _weak_folder_title_keys(context)
    candidate_title_keys = {
        _comparison_key(value) for value in (title, original, *aliases) if value
    }
    if (
        not rejected
        and high_information_anchors
        and (
            _low_information_query(matched_query)
            or bool(weak_folder_keys.intersection(candidate_title_keys))
        )
        and primary_title_score < 0.9
    ):
        rejected.append("low_information_variant_match")
        constraint_penalty = -0.35

    matched_query_key = _comparison_key(matched_query)
    if (
        not rejected
        and _ambiguous_latin_ordinal_attack_alias(
            anchor_queries, [title, original, *aliases], context.season,
        )
    ):
        rejected.append("ambiguous_ordinal_attack_alias")
        constraint_penalty = -0.35
    if (
        not rejected
        and _has_protected_short_title_suffix(
            anchor_queries, [title, original, *aliases]
        )
    ):
        rejected.append("protected_title_suffix_missing")
        constraint_penalty = -0.35
    if (
        not rejected
        and matched_query_key not in anchor_keys
        and best_title_score >= 0.9
        and primary_title_score < 0.9
        and best_title_score - primary_title_score >= 0.08
        and _has_distinctive_title_remainder(anchor_queries, matched_query)
        and not _explicit_quoted_title_matches(anchor_queries, matched_query)
        and not _known_season_alias_remainder(
            anchor_queries, matched_query, context.season,
        )
        and not _primary_title_parts_covered(
            anchor_queries, [title, original, *aliases]
        )
    ):
        rejected.append("distinctive_title_tokens_missing")
        constraint_penalty = -0.12

    if "media_type_mismatch" in rejected:
        final_score = 0.0
    elif expected_year:
        final_score = best_title_score * 0.78 + year_score * 0.17 + media_type_score * 0.05
        final_score += year_penalty + constraint_penalty
    else:
        final_score = best_title_score * 0.92 + media_type_score * 0.08
        final_score += constraint_penalty

    # 季度线索只在没有被拒绝的候选之间做重排，且只加分不罚分：季度标签
    # 与 TMDB 季首播月存在合法偏差，缺席不应被当成反证。
    quarter_bonus = 0.0
    if not rejected:
        source_quarter = parse_release_quarter(context.filename, context.parent_path)
        if source_quarter:
            candidate_quarter = _air_date_quarter(
                (season_year_evidence or {}).get("season_air_date") or candidate_date
            )
            if candidate_quarter and candidate_quarter == source_quarter:
                quarter_bonus = _QUARTER_BONUS
                final_score += quarter_bonus
    final_score = round(max(0.0, min(1.0, final_score)), 3)
    return CandidateScoreBreakdown(
        title_score=float(round(title_score, 3)),
        original_title_score=float(round(original_score, 3)),
        alias_score=float(round(alias_score, 3)),
        year_score=float(round(year_score, 3)),
        year_penalty=float(round(year_penalty, 3)),
        media_type_score=float(round(media_type_score, 3)),
        constraint_penalty=float(round(constraint_penalty, 3)),
        quarter_bonus=float(round(quarter_bonus, 3)),
        final_score=float(final_score),
        matched_title=matched_title,
        matched_query=matched_query,
        rejected_constraints=rejected,
    )


def decide_threshold(score: float, threshold: float,
                     rejected_constraints: list[str] | None = None) -> dict[str, object]:
    """阶段 5：生成可解析的最终阈值决定。"""
    rejected = list(rejected_constraints or [])
    passed = float(score) >= float(threshold) and not rejected
    reason = "score_met" if passed else (rejected[0] if rejected else "below_threshold")
    return {
        "threshold": float(round(threshold, 3)),
        "score": float(round(score, 3)),
        "passed": passed,
        "reason": reason,
    }


VERIFIED_AUTOMATIC_IDENTITY_PROOF_KEY = "verified_automatic_identity_proof"
_VERIFIED_AUTOMATIC_IDENTITY_PROOF_VERSION = 2
_VERIFIED_AUTOMATIC_IDENTITY_MIN_CONFIDENCE = 0.82
_VERIFIED_AUTOMATIC_IDENTITY_RECOGNITION_MAX_THRESHOLD = 0.9
_VERIFIED_AUTOMATIC_IDENTITY_MIN_CANDIDATE_GAP = 0.08


def verified_automatic_identity_proof(
    match: MatchResult | None,
    *,
    global_threshold: float | None = None,
    preset: str | None = None,
) -> dict[str, object] | None:
    """读取并复核可供自动整理消费的剧集身份强证据。

    证明绑定生成时的自动安全预设与阈值。保守预设不会被历史 90% 证明
    绕过；积极预设也只允许在自身阈值以下、且具备唯一标题锚点和已验证
    TMDB 季集位置的窄范围结果。任一字段缺失或不一致都会失败关闭。
    """
    if match is None:
        return None
    expected_policy = automatic_match_policy(preset)
    expected_threshold = (
        expected_policy.threshold
        if global_threshold is None
        else float(global_threshold)
    )
    if (
        not math.isfinite(expected_threshold)
        or not 0.0 < expected_threshold <= 1.0
        or abs(expected_threshold - expected_policy.threshold) > 0.001
    ):
        return None
    proof_ceiling = min(
        expected_threshold,
        _VERIFIED_AUTOMATIC_IDENTITY_RECOGNITION_MAX_THRESHOLD,
    )
    metadata = getattr(match, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    proof = metadata.get(VERIFIED_AUTOMATIC_IDENTITY_PROOF_KEY)
    if not isinstance(proof, dict):
        return None
    if proof.get("version") != _VERIFIED_AUTOMATIC_IDENTITY_PROOF_VERSION:
        return None
    if str(proof.get("kind") or "") != "tmdb_tv_episode_identity":
        return None
    if str(proof.get("provider") or "").strip().lower() != "tmdb":
        return None
    if str(proof.get("media_type") or "").strip().lower() != "tv":
        return None
    if str(getattr(match, "media_type", "") or "").strip().lower() != "tv":
        return None

    proof_id = str(proof.get("external_id") or "").strip()
    match_id = str(
        getattr(match, "external_id", "")
        or getattr(match, "tmdb_id", "")
        or ""
    ).strip()
    if not proof_id or proof_id != match_id:
        return None
    provider = str(getattr(match, "provider", "") or "").strip().lower()
    if provider and provider != "tmdb":
        return None

    try:
        confidence = float(getattr(match, "confidence", 0.0) or 0.0)
        match_threshold = float(getattr(match, "threshold", 0.0) or 0.0)
        proof_confidence = float(proof.get("confidence") or 0.0)
        recognition_threshold = float(proof.get("recognition_threshold") or 0.0)
        proof_global_threshold = float(proof.get("global_threshold") or 0.0)
        candidate_count = int(proof.get("candidate_count") or 0)
        candidate_gap = float(proof.get("candidate_gap") or 0.0)
        strong_title_score = float(proof.get("strong_title_score") or 0.0)
    except (TypeError, ValueError):
        return None
    finite_values = (
        confidence, match_threshold, proof_confidence, recognition_threshold,
        proof_global_threshold, candidate_gap, strong_title_score,
    )
    if not all(math.isfinite(value) for value in finite_values):
        return None
    if not all(0.0 <= value <= 1.0 for value in finite_values):
        return None
    if abs(confidence - proof_confidence) > 0.001:
        return None
    # MatchResult.threshold 记录本次识别模式（strict=0.9 / loose=0.6），
    # proof.global_threshold 才记录自动整理安全档位。两者语义不同，但都
    # 必须与生成时元数据精确绑定，防止证明被跨预设复用。
    if (
        match_threshold <= 0
        or match_threshold
        > _VERIFIED_AUTOMATIC_IDENTITY_RECOGNITION_MAX_THRESHOLD
    ):
        return None
    if abs(match_threshold - recognition_threshold) > 0.001:
        return None
    if not (
        _VERIFIED_AUTOMATIC_IDENTITY_MIN_CONFIDENCE
        <= confidence
        < proof_ceiling
    ):
        return None
    if abs(proof_global_threshold - expected_threshold) > 0.001:
        return None
    if normalize_automatic_match_preset(
        proof.get("automatic_match_preset")
    ) != expected_policy.name:
        return None
    if str(proof.get("automatic_match_preset") or "").strip().lower() \
            != expected_policy.name:
        return None
    if strong_title_score < _VERIFIED_AUTOMATIC_IDENTITY_MIN_CONFIDENCE:
        return None
    if candidate_count < 1:
        return None
    if candidate_count > 1 and candidate_gap < _VERIFIED_AUTOMATIC_IDENTITY_MIN_CANDIDATE_GAP:
        return None
    if list(proof.get("decision_constraints") or []):
        return None
    if list(proof.get("selected_constraints") or []):
        return None

    source_position = proof.get("source_position")
    target_position = proof.get("target_position")
    validation = proof.get("position_validation")
    if not all(isinstance(item, dict) for item in (source_position, target_position, validation)):
        return None
    try:
        source_season_raw = source_position.get("season")
        source_season = (
            None if source_season_raw is None else int(source_season_raw)
        )
        source_episode = int(source_position.get("episode"))
        target_season = int(target_position.get("season"))
        target_episode = int(target_position.get("episode"))
    except (TypeError, ValueError):
        return None

    expected_year = str(proof.get("expected_year") or "").strip()
    candidate_year = str(proof.get("candidate_year") or "").strip()
    season_year_evidence = proof.get("target_season_year_evidence")
    validated_season_year_evidence = None
    if season_year_evidence is not None or (expected_year and candidate_year != expected_year):
        validated_season_year_evidence = _validated_target_season_year_proof_evidence(
            season_year_evidence,
            tmdb_id=proof_id,
            expected_year=expected_year,
            source_season=source_season,
            source_episode=source_episode,
            target_season=target_season,
            target_episode=target_episode,
            proof_position_validation=validation,
            proof_episode_mapping=proof.get("episode_mapping"),
        )
        if validated_season_year_evidence is None:
            return None
    if source_season == 0 or source_episode < 1:
        return None
    if target_season < 1 or target_episode < 1:
        return None
    if not bool(validation.get("required")) or not bool(validation.get("passed")):
        return None
    if str(validation.get("reason") or "") != "episode_verified":
        return None
    try:
        if int(validation.get("season")) != target_season:
            return None
        if int(validation.get("episode")) != target_episode:
            return None
    except (TypeError, ValueError):
        return None
    return dict(proof)


def _verified_automatic_identity_precheck(
    *,
    result: RecognitionResult,
    context: RecognitionContext,
    scored: list[tuple[CandidateScoreBreakdown, dict]],
    decision_constraints: list[str],
    expected_year: str,
    global_threshold: float | None = None,
    preset: str | None = None,
) -> bool:
    """判断是否值得为低于安全档位的 TV 候选验证季集。"""
    policy = automatic_match_policy(preset)
    threshold = policy.threshold if global_threshold is None else float(global_threshold)
    if (
        not math.isfinite(threshold)
        or abs(threshold - policy.threshold) > 0.001
    ):
        return False
    proof_ceiling = min(
        threshold, _VERIFIED_AUTOMATIC_IDENTITY_RECOGNITION_MAX_THRESHOLD
    )
    if result.media_type != "tv" or context.episode is None or context.episode < 1:
        return False
    if context.season == 0 or _low_information_query(context.normalized_title):
        return False
    if not (
        _VERIFIED_AUTOMATIC_IDENTITY_MIN_CONFIDENCE
        <= result.confidence
        < proof_ceiling
    ):
        return False
    if decision_constraints or not scored:
        return False
    selected = scored[0][0]
    if selected.rejected_constraints:
        return False
    strong_title_score = max(
        selected.title_score, selected.original_title_score, selected.alias_score
    )
    if strong_title_score < _VERIFIED_AUTOMATIC_IDENTITY_MIN_CONFIDENCE:
        return False
    selected_raw = scored[0][1]
    season_year_evidence = _validated_target_season_year_evidence(
        selected_raw, context, expected_year,
    )
    if (
        expected_year
        and str(result.year or "").strip() != expected_year
        and season_year_evidence is None
    ):
        return False
    if len(scored) > 1:
        candidate_gap = result.confidence - float(scored[1][0].final_score)
        if candidate_gap < _VERIFIED_AUTOMATIC_IDENTITY_MIN_CANDIDATE_GAP:
            return False
    return True


def _build_verified_automatic_identity_proof(
    *,
    result: RecognitionResult,
    context: RecognitionContext,
    scored: list[tuple[CandidateScoreBreakdown, dict]],
    decision_constraints: list[str],
    mapping: EpisodeMappingPlan,
    position: dict[str, object],
    expected_year: str,
) -> dict[str, object] | None:
    """生成每个文件独立的低分强证据；不得跨目录文件复用。"""
    policy = automatic_match_policy()
    if not _verified_automatic_identity_precheck(
        result=result, context=context, scored=scored,
        decision_constraints=decision_constraints, expected_year=expected_year,
        global_threshold=policy.threshold, preset=policy.name,
    ):
        return None
    if not bool(position.get("required")) or not bool(position.get("passed")):
        return None
    if str(position.get("reason") or "") != "episode_verified":
        return None
    # 裸集号（Title - 06）在现有 TMDB 校验与 Organizer 中都按 S01
    # 解释。mapping 的 identity 结果会保留 season=None，因此 proof 必须
    # 采用已经通过 TMDB 校验的有效目标季号，避免同一语义在证明层自我否决。
    target_season_raw = (
        mapping.target_season
        if mapping.target_season is not None
        else position.get("season")
    )
    target_episode_raw = (
        mapping.target_episode
        if mapping.target_episode is not None
        else position.get("episode")
    )
    try:
        target_season = int(target_season_raw)
        target_episode = int(target_episode_raw)
    except (TypeError, ValueError):
        return None
    if target_season < 1 or target_episode < 1:
        return None
    selected = scored[0][0]
    selected_raw = scored[0][1]
    season_year_evidence = _validated_target_season_year_evidence(
        selected_raw, context, expected_year,
    )
    if season_year_evidence is not None:
        if mapping.changed:
            return None
        try:
            if int(season_year_evidence.get("target_season")) != target_season:
                return None
            if int(season_year_evidence.get("target_episode")) != target_episode:
                return None
        except (TypeError, ValueError):
            return None
    strong_title_score = max(
        selected.title_score, selected.original_title_score, selected.alias_score
    )
    candidate_gap = (
        1.0
        if len(scored) == 1
        else max(0.0, result.confidence - float(scored[1][0].final_score))
    )
    proof = {
        "version": _VERIFIED_AUTOMATIC_IDENTITY_PROOF_VERSION,
        "kind": "tmdb_tv_episode_identity",
        "provider": "tmdb",
        "external_id": str(result.tmdb_id or ""),
        "media_type": "tv",
        "confidence": float(round(result.confidence, 3)),
        "recognition_threshold": float(round(result.threshold, 3)),
        "automatic_match_preset": policy.name,
        "global_threshold": float(round(policy.threshold, 3)),
        "strong_title_score": float(round(strong_title_score, 3)),
        "candidate_count": len(scored),
        "candidate_gap": float(round(candidate_gap, 3)),
        "decision_constraints": list(decision_constraints),
        "selected_constraints": list(selected.rejected_constraints),
        "expected_year": str(expected_year or ""),
        "candidate_year": str(result.year or ""),
        "source_title_key": _comparison_key(context.normalized_title),
        "matched_title_key": _comparison_key(selected.matched_title),
        "source_position": {
            "season": context.season,
            "episode": context.episode,
        },
        "target_position": {
            "season": target_season,
            "episode": target_episode,
        },
        "position_validation": dict(position),
        "episode_mapping": mapping.to_dict(),
    }
    if season_year_evidence is not None:
        proof["target_season_year_evidence"] = season_year_evidence
    return proof


class TMDBScraper:
    supports_parent_path = True


    def close(self) -> bool:
        """释放内部创建的 TMDB Client；注入 Client 仍由调用方管理。"""
        with self._close_lock:
            if self._closed:
                return True
            if self._owns_client and not close_tmdb_client(self.client):
                return False
            self._closed = True
            return True

    @staticmethod
    def validate_position(
        detail: dict, media_type: str, season: int | None, episode: int | None,
    ) -> dict[str, object]:
        """供整理层复核最终季集；目录身份缓存和预处理不得绕过此门禁。"""
        return _validate_tmdb_position(detail, media_type, season, episode)

    @staticmethod
    def position_validation_error(validation: dict[str, object]) -> str:
        return _tmdb_position_error(validation)

    def __init__(self, client: TMDBClient | None = None):
        self._owns_client = client is None
        self.client = client or TMDBClient()
        self._close_lock = threading.Lock()
        self._closed = False
        self.api_key = str(getattr(self.client, "api_key", "") or "")
        self.base_url = str(getattr(self.client, "base_url", "") or "").rstrip("/")
        self.match_mode = get("TMDB_MATCH_MODE", "strict")  # strict / loose
        self._last_search_error = ""
        self._last_search_status = ""
        self._last_search_cache_hit = False
        self._last_search_empty_cache_hit = False
        self._search_outcome_local = threading.local()
        self._recognition_detail_cache: OrderedDict[
            tuple[str, str], dict
        ] = OrderedDict()
        self._recognition_detail_failures: set[tuple[str, str]] = set()
        self._detail_cache: dict[tuple[str, str], dict] = {}
        self._credits_detail_cache: dict[tuple[str, str], dict] = {}
        self._season_detail_cache: dict[tuple[str, int], dict] = {}
        self._search_cache: dict[tuple[str, str, str], tuple[float, list[dict]]] = {}
        self._search_failure_cache: dict[tuple[str, str, str], tuple[float, str]] = {}
        self._detail_failure_cache: dict[tuple[str, str], tuple[float, str]] = {}
        self._season_detail_failure_cache: dict[tuple[str, int], tuple[float, str]] = {}
        self._tmdb_consecutive_failures = 0
        self._tmdb_circuit_open_until = 0.0
        self._tmdb_state_lock = threading.RLock()
        self._ai_lock = threading.RLock()
        self._ai_clients: dict[tuple[str, int, int], AIRecognitionClient] = {}
        self._ai_result_cache: dict[str, tuple[float, AIRecognitionResult]] = {}
        self._release_group_ai_cache: dict[str, tuple[float, AIReleaseGroupResult]] = {}
        self._ai_inflight: dict[str, threading.Event] = {}
        self._release_group_ai_inflight: dict[str, threading.Event] = {}
        self._ai_failure_cache: OrderedDict[
            str, tuple[float, str, str]
        ] = OrderedDict()
        self._release_group_ai_failure_cache: OrderedDict[
            str, tuple[float, str, str]
        ] = OrderedDict()
        self._performance_counters = {
            "tmdb_search_requests": 0,
            "tmdb_search_cache_hits": 0,
            "tmdb_detail_requests": 0,
            "tmdb_detail_cache_hits": 0,
            "tmdb_season_detail_requests": 0,
            "tmdb_season_detail_cache_hits": 0,
            "ai_requests": 0,
            "tmdb_failure_cache_hits": 0,
            "tmdb_circuit_rejections": 0,
            "ai_cache_hits": 0,
            "tavily_hint_lookups": 0,
            "tavily_hint_requests": 0,
            "tavily_hint_cache_hits": 0,
            "tavily_hint_matches": 0,
        }
        # 保留旧属性，兼容现有调试/测试代码；实际请求由公共 Client 负责。
        self._session = getattr(self.client, "session", None)

    # ===== TMDB API =====
    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        return self.client.get(path, params)

    def performance_snapshot(self) -> dict[str, int]:
        """返回累计只读性能计数；调用方可用前后快照计算本次任务增量。"""
        with self._tmdb_state_lock, self._ai_lock:
            return {key: int(value) for key, value in self._performance_counters.items()}

    @staticmethod
    def _clone_search_results(results: list[dict]) -> list[dict]:
        return [dict(item) for item in results if isinstance(item, dict)]

    @staticmethod
    def _transient_tmdb_error(exc: Exception) -> bool:
        return isinstance(exc, (ProviderUnavailable, ProviderTimeout, ProviderRateLimited))

    def _record_tmdb_success(self) -> None:
        with self._tmdb_state_lock:
            self._tmdb_consecutive_failures = 0
            self._tmdb_circuit_open_until = 0.0

    def _record_tmdb_failure(self, exc: Exception) -> None:
        if not self._transient_tmdb_error(exc):
            return
        with self._tmdb_state_lock:
            self._tmdb_consecutive_failures += 1
            if self._tmdb_consecutive_failures >= _TMDB_CIRCUIT_THRESHOLD:
                retry_after = int(getattr(exc, "retry_after", 0) or 0)
                self._tmdb_circuit_open_until = time.monotonic() + max(
                    _TMDB_CIRCUIT_COOLDOWN_SECONDS, float(retry_after),
                )

    def _tmdb_circuit_blocked(self) -> bool:
        with self._tmdb_state_lock:
            if self._tmdb_circuit_open_until <= time.monotonic():
                return False
            self._performance_counters["tmdb_circuit_rejections"] += 1
            return True

    def _active_tmdb_failure(self, cache: dict, key) -> str:
        now = time.monotonic()
        with self._tmdb_state_lock:
            failed = cache.get(key)
            if failed and now - failed[0] <= _TMDB_FAILURE_CACHE_TTL_SECONDS:
                self._performance_counters["tmdb_failure_cache_hits"] += 1
                return str(failed[1] or "")
            cache.pop(key, None)
        return ""

    def _remember_tmdb_failure(self, cache: dict, key, message: str) -> None:
        now = time.monotonic()
        with self._tmdb_state_lock:
            expired = [
                item_key for item_key, value in cache.items()
                if now - float(value[0]) > _TMDB_FAILURE_CACHE_TTL_SECONDS
            ]
            for item_key in expired:
                cache.pop(item_key, None)
            if key not in cache and len(cache) >= _TMDB_FAILURE_CACHE_LIMIT:
                cache.pop(next(iter(cache)))
            cache[key] = (now, str(message or "")[:500])

    def _remember_recognition_detail(
        self, key: tuple[str, str], detail: dict, *, failed: bool,
    ) -> None:
        with self._tmdb_state_lock:
            self._recognition_detail_cache.pop(key, None)
            self._recognition_detail_cache[key] = dict(detail)
            if failed:
                self._recognition_detail_failures.add(key)
            else:
                self._recognition_detail_failures.discard(key)
            while (
                len(self._recognition_detail_cache)
                > _RECOGNITION_DETAIL_CACHE_LIMIT
            ):
                evicted_key, _value = self._recognition_detail_cache.popitem(
                    last=False
                )
                self._recognition_detail_failures.discard(evicted_key)

    def _cached_recognition_detail(
        self, key: tuple[str, str],
    ) -> tuple[bool, dict, bool]:
        with self._tmdb_state_lock:
            detail = self._recognition_detail_cache.get(key)
            if detail is None and key not in self._recognition_detail_cache:
                return False, {}, False
            self._recognition_detail_cache.move_to_end(key)
            return (
                True,
                dict(detail or {}),
                key in self._recognition_detail_failures,
            )

    @staticmethod
    def _active_ai_failure(
        cache: OrderedDict[str, tuple[float, str, str]],
        key: str,
        now: float,
    ) -> tuple[float, str, str] | None:
        failed = cache.get(key)
        if failed is None:
            return None
        if now - failed[0] > _AI_FAILURE_CACHE_TTL_SECONDS:
            cache.pop(key, None)
            return None
        cache.move_to_end(key)
        return failed

    @staticmethod
    def _remember_ai_failure(
        cache: OrderedDict[str, tuple[float, str, str]],
        key: str,
        error: str,
        kind: str,
        *,
        now: float | None = None,
    ) -> None:
        current = time.monotonic() if now is None else float(now)
        expired = [
            item_key
            for item_key, value in cache.items()
            if current - value[0] > _AI_FAILURE_CACHE_TTL_SECONDS
        ]
        for item_key in expired:
            cache.pop(item_key, None)
        cache.pop(key, None)
        cache[key] = (current, str(error or "")[:500], str(kind or ""))
        while len(cache) > _AI_FAILURE_CACHE_LIMIT:
            cache.popitem(last=False)

    def _publish_search_outcome(self, outcome: _SearchOutcome) -> None:
        """发布兼容诊断字段；识别管线使用线程内 outcome，避免并发串扰。"""
        with self._tmdb_state_lock:
            self._last_search_status = outcome.status
            self._last_search_error = outcome.error
            self._last_search_cache_hit = outcome.cache_hit
            self._last_search_empty_cache_hit = outcome.empty_cache_hit
        self._search_outcome_local.value = outcome

    def _clear_thread_search_outcome(self) -> None:
        if hasattr(self._search_outcome_local, "value"):
            del self._search_outcome_local.value

    def _take_thread_search_outcome(self, results: list[dict]) -> _SearchOutcome:
        outcome = getattr(self._search_outcome_local, "value", None)
        self._clear_thread_search_outcome()
        if isinstance(outcome, _SearchOutcome):
            return outcome
        # 测试或扩展代码可能替换 public search 方法；此时只能基于返回值
        # 生成保守的调用局部结果，但绝不借用其它调用遗留的实例状态。
        cloned = tuple(self._clone_search_results(results))
        return _SearchOutcome(
            results=cloned,
            status="matched" if cloned else "no_result",
        )

    def _search_with_outcome(
        self, title: str, year: str, media_type: str,
    ) -> _SearchOutcome:
        normalized_type = "tv" if media_type == "tv" else "movie"
        normalized_title = " ".join(str(title or "").split()).casefold()
        normalized_year = str(year or "").strip()
        cache_key = (normalized_type, normalized_title, normalized_year)
        with self._tmdb_state_lock:
            cached = self._search_cache.get(cache_key)
            if cached:
                cached_at, cached_results = cached
                ttl = (
                    _SEARCH_CACHE_TTL_SECONDS
                    if cached_results else _EMPTY_SEARCH_CACHE_TTL_SECONDS
                )
                if time.monotonic() - cached_at <= ttl:
                    self._performance_counters["tmdb_search_cache_hits"] += 1
                    cloned = tuple(self._clone_search_results(cached_results))
                    return _SearchOutcome(
                        results=cloned,
                        status="matched" if cloned else "no_result",
                        cache_hit=True,
                        empty_cache_hit=not bool(cloned),
                    )
                self._search_cache.pop(cache_key, None)
        failed_message = self._active_tmdb_failure(
            self._search_failure_cache, cache_key
        )
        if failed_message:
            return _SearchOutcome(status="request_error", error=failed_message)
        if self._tmdb_circuit_blocked():
            return _SearchOutcome(
                status="request_error",
                error="TMDB 服务暂时不可用，已进入短时保护",
            )

        raw_config_error = getattr(self.client, "config_error", "")
        config_error = raw_config_error.strip() if isinstance(raw_config_error, str) else ""
        if config_error:
            logger.error(config_error)
            return _SearchOutcome(status="config_error", error=config_error)
        if not self.api_key:
            error = "未配置 TMDB_API_KEY"
            logger.error(error)
            return _SearchOutcome(status="config_error", error=error)
        try:
            with self._tmdb_state_lock:
                self._performance_counters["tmdb_search_requests"] += 1
            results = self._clone_search_results(
                self.client.search(title, year, normalized_type)
            )
            self._record_tmdb_success()
            with self._tmdb_state_lock:
                if (
                    cache_key not in self._search_cache
                    and len(self._search_cache) >= _SEARCH_CACHE_LIMIT
                ):
                    self._search_cache.pop(next(iter(self._search_cache)))
                self._search_cache[cache_key] = (
                    time.monotonic(), self._clone_search_results(results),
                )
            cloned = tuple(self._clone_search_results(results))
            return _SearchOutcome(
                results=cloned,
                status="matched" if cloned else "no_result",
            )
        except Exception as e:
            error = redact_sensitive_text(f"TMDB 请求失败：{e}")[:500]
            if self._transient_tmdb_error(e):
                self._remember_tmdb_failure(
                    self._search_failure_cache, cache_key, error
                )
                self._record_tmdb_failure(e)
            logger.error(
                "TMDB 搜索失败 [%s] [%s] [%s]: %s",
                redact_sensitive_text(title), year, media_type, redact_sensitive_text(e),
            )
            return _SearchOutcome(status="request_error", error=error)

    def search(self, title: str, year: str, media_type: str) -> list[dict]:
        outcome = self._search_with_outcome(title, year, media_type)
        self._publish_search_outcome(outcome)
        return self._clone_search_results(list(outcome.results))

    @staticmethod
    def _remember_detail(
        cache: dict[tuple[str, str], dict],
        key: tuple[str, str],
        detail: dict,
        *,
        limit: int = 128,
    ) -> None:
        if not isinstance(detail, dict) or not detail:
            return
        if key not in cache and len(cache) >= limit:
            cache.pop(next(iter(cache)))
        cache[key] = dict(detail)

    def get_detail(
        self, tmdb_id: str, media_type: str, *, force_refresh: bool = False
    ) -> dict:
        """获取 TMDB 详情；成功结果在当前进程内做有界复用。

        ``force_refresh`` 仅供最终季集边界校验失败时做一次受控复核，
        避免进程内陈旧的季集缓存长期把新上线剧集判为越界。
        """
        if not tmdb_id:
            return {}
        normalized = "tv" if media_type == "tv" else "movie"
        key = (normalized, str(tmdb_id).strip())
        if force_refresh:
            with self._tmdb_state_lock:
                self._detail_cache.pop(key, None)
                self._credits_detail_cache.pop(key, None)
                self._recognition_detail_cache.pop(key, None)
                self._recognition_detail_failures.discard(key)
                self._detail_failure_cache.pop(key, None)
        cached = self._credits_detail_cache.get(key) or self._detail_cache.get(key)
        if cached:
            self._performance_counters["tmdb_detail_cache_hits"] += 1
            return dict(cached)
        if self._active_tmdb_failure(self._detail_failure_cache, key):
            return {}
        if self._tmdb_circuit_blocked():
            return {}
        try:
            self._performance_counters["tmdb_detail_requests"] += 1
            detail = self.client.detail(key[1], normalized)
            self._record_tmdb_success()
            self._remember_detail(self._detail_cache, key, detail)
            return dict(detail) if isinstance(detail, dict) else {}
        except Exception as e:
            if self._transient_tmdb_error(e):
                self._remember_tmdb_failure(
                    self._detail_failure_cache, key, redact_sensitive_text(e)[:500]
                )
                self._record_tmdb_failure(e)
            logger.error("TMDB 详情失败 tmdb=%s type=%s error=%s", tmdb_id, normalized, type(e).__name__)
            return {}

    def _season_episodes(self, tmdb_id: str, season_number: int | None) -> list | None:
        """读取季逐集清单；接口失败按证据不足处理，不得据此淘汰候选。"""
        if season_number is None:
            return None
        try:
            season_detail = self.get_tv_season_detail(tmdb_id, int(season_number))
        except Exception:
            return None
        episodes = (
            season_detail.get("episodes") if isinstance(season_detail, dict) else None
        )
        return episodes if isinstance(episodes, list) else None

    def get_tv_season_detail(
        self, tmdb_id: str, season_number: int, *, force_refresh: bool = False
    ) -> dict:
        """获取 TV 季逐集详情；供严格的发布季/TMDB 合并季映射复用。"""
        if (
            not tmdb_id
            or isinstance(season_number, bool)
            or not isinstance(season_number, int)
            or not 0 <= season_number <= 99
        ):
            return {}
        key = (str(tmdb_id).strip(), int(season_number))
        if force_refresh:
            with self._tmdb_state_lock:
                self._season_detail_cache.pop(key, None)
                self._season_detail_failure_cache.pop(key, None)
        cached = self._season_detail_cache.get(key)
        if cached:
            self._performance_counters["tmdb_season_detail_cache_hits"] += 1
            return dict(cached)
        if self._active_tmdb_failure(self._season_detail_failure_cache, key):
            return {}
        if self._tmdb_circuit_blocked():
            return {}
        try:
            self._performance_counters["tmdb_season_detail_requests"] += 1
            detail = self.client.tv_season_detail(key[0], key[1])
            self._record_tmdb_success()
            if isinstance(detail, dict) and detail:
                if (
                    key not in self._season_detail_cache
                    and len(self._season_detail_cache) >= 128
                ):
                    self._season_detail_cache.pop(next(iter(self._season_detail_cache)))
                self._season_detail_cache[key] = dict(detail)
                return dict(detail)
            return {}
        except Exception as e:
            if self._transient_tmdb_error(e):
                self._remember_tmdb_failure(
                    self._season_detail_failure_cache,
                    key,
                    redact_sensitive_text(f"{e}")[:500],
                )
                self._record_tmdb_failure(e)
            logger.error(
                "TMDB 季详情失败 [%s] [S%02d]: %s", tmdb_id, season_number, e
            )
            return {}

    def get_detail_with_credits(self, tmdb_id: str, media_type: str) -> dict:
        """获取候选详情及演职员；搜索与预览复用同一成功响应。"""
        if not tmdb_id:
            return {}
        normalized = "tv" if media_type == "tv" else "movie"
        key = (normalized, str(tmdb_id).strip())
        cached = self._credits_detail_cache.get(key)
        if cached:
            self._performance_counters["tmdb_detail_cache_hits"] += 1
            return dict(cached)
        if self._active_tmdb_failure(self._detail_failure_cache, key):
            return {}
        if self._tmdb_circuit_blocked():
            return {}
        try:
            self._performance_counters["tmdb_detail_requests"] += 1
            detail = self._get(
                f"/{normalized}/{key[1]}",
                {"append_to_response": "credits"},
            )
            self._record_tmdb_success()
            self._remember_detail(self._credits_detail_cache, key, detail)
            self._remember_detail(self._detail_cache, key, detail)
            return dict(detail) if isinstance(detail, dict) else {}
        except Exception as e:
            if self._transient_tmdb_error(e):
                self._remember_tmdb_failure(
                    self._detail_failure_cache, key, redact_sensitive_text(e)[:500]
                )
                self._record_tmdb_failure(e)
            logger.error("TMDB 演职员详情失败 tmdb=%s type=%s error=%s", tmdb_id, normalized, type(e).__name__)
            return {}

    def search_candidates(self, query: str, year: str = "",
                          media_type: str = "movie") -> list[Candidate]:
        """人工纠偏搜索入口，返回按标题和年份排序后的 Top3。"""
        media_type = "tv" if media_type == "tv" else "movie"
        clean = self.clean_title(query)
        if not clean:
            return []
        candidates = self.search(clean, year, media_type)
        if not candidates and year:
            candidates = self.search(clean, "", media_type)
        if not candidates:
            return []
        return self._pick_best(clean, year, media_type, candidates).candidates

    def match_from_tmdb(self, tmdb_id: str, media_type: str) -> MatchResult:
        """按人工指定的 TMDB ID 构建完整匹配结果，并绑定详情响应身份。"""
        media_type = "tv" if media_type == "tv" else "movie"
        requested_id = str(tmdb_id or "").strip()
        detail = self.get_detail(requested_id, media_type)
        if not detail:
            return MatchResult(
                media_type=media_type, need_confirm=True, error="TMDB 详情不存在",
                status="request_error", matched_by="tmdb_id", threshold=1.0,
            )
        detail_id = str(detail.get("id") or "").strip()
        if not requested_id or detail_id != requested_id:
            return MatchResult(
                tmdb_id=requested_id, external_id=requested_id, provider="tmdb",
                media_type=media_type, need_confirm=True,
                error=(
                    f"TMDB 详情身份不一致：请求 {requested_id or '为空'}，"
                    f"响应 {detail_id or '缺少 ID'}"
                ),
                status="request_error", matched_by="tmdb_id", threshold=1.0,
            )
        title = str(detail.get("name") or detail.get("title") or "")
        date = str(detail.get("first_air_date") or detail.get("release_date") or "")
        return MatchResult(
            tmdb_id=requested_id, external_id=requested_id, provider="tmdb",
            title=title, year=date[:4], media_type=media_type,
            confidence=1.0, locked=True, status="matched", matched_by="tmdb_id",
            threshold=1.0,
            metadata={
                "original_title": str(
                    detail.get("original_name") or detail.get("original_title") or ""
                ),
                "poster_path": str(detail.get("poster_path") or ""),
                "backdrop_path": str(detail.get("backdrop_path") or ""),
            },
        )

    @staticmethod
    def _explicit_match_evidence(
        result: MatchResult, context: RecognitionContext,
    ) -> dict[str, object]:
        matched = bool(
            result.status == "matched" and result.tmdb_id and result.title
        )
        anchors = _source_title_anchors(context)
        metadata = result.metadata if isinstance(result.metadata, dict) else {}
        candidate_titles = _unique_text((
            result.title, str(metadata.get("original_title") or ""),
        ))
        title_score = (
            max(
                _title_similarity_score(anchor, candidate_title)
                for anchor in anchors
                for candidate_title in candidate_titles
            )
            if anchors and candidate_titles
            else (1.0 if matched and not anchors else 0.0)
        )
        expected_year = str(
            context.folder_year or context.filename_year or ""
        ).strip()
        result_year = str(result.year or "").strip()
        year_distance: int | None = None
        if expected_year and result_year and expected_year.isdigit() and result_year.isdigit():
            year_distance = abs(int(expected_year) - int(result_year))
        year_matches = not expected_year or result_year == expected_year
        # 电影首映、地区发行与流媒体年份偶尔会相差一年。显式 ID 的类型
        # 复核只把这种差异视为可接受，不放宽标题证据或更大的年份冲突。
        year_compatible = bool(
            not expected_year or year_matches or year_distance == 1
        )
        position_matches = (
            result.media_type == "tv"
            or (context.season is None and context.episode is None)
        )
        qualified = bool(
            matched
            and position_matches
            and year_compatible
            and (not anchors or title_score >= 0.72)
        )
        rank = title_score
        if expected_year and year_matches:
            rank += 0.2
        elif expected_year and year_distance == 1:
            rank += 0.1
        if position_matches:
            rank += 0.05
        return {
            "matched": matched,
            "qualified": qualified,
            "title_score": title_score,
            "year_matches": year_matches,
            "year_compatible": year_compatible,
            "year_distance": year_distance,
            "expected_year": expected_year,
            "rank": rank,
        }

    @staticmethod
    def _explicit_result_candidate(
        result: MatchResult, evidence: dict[str, object],
    ) -> Candidate | None:
        if not evidence.get("matched"):
            return None
        metadata = result.metadata if isinstance(result.metadata, dict) else {}
        return Candidate(
            tmdb_id=str(result.tmdb_id or ""),
            title=str(result.title or ""),
            year=str(result.year or ""),
            score=round(min(float(evidence.get("rank") or 0.0), 1.0), 3),
            media_type=str(result.media_type or ""),
            original_title=str(metadata.get("original_title") or ""),
            poster_path=str(metadata.get("poster_path") or ""),
            backdrop_path=str(metadata.get("backdrop_path") or ""),
            provider="tmdb",
            external_id=str(result.tmdb_id or ""),
        )

    def _match_inherited_tmdb(
        self, tmdb_id: str, primary_type: str, context: RecognitionContext,
    ) -> MatchResult:
        """复核父目录继承的数字 ID，避免 Movie/TV 同号被错误类型吞掉。"""
        primary_type = "tv" if primary_type == "tv" else "movie"
        primary = self.match_from_tmdb(tmdb_id, primary_type)
        primary_evidence = self._explicit_match_evidence(primary, context)
        if primary_evidence["qualified"]:
            return primary

        alternate_type = "movie" if primary_type == "tv" else "tv"
        alternate = self.match_from_tmdb(tmdb_id, alternate_type)
        alternate_evidence = self._explicit_match_evidence(alternate, context)
        if alternate_evidence["qualified"]:
            return alternate

        # 只有一个命名空间存在时不存在 Movie/TV 同号碰撞，继续尊重已有的
        # 显式 TMDB 标记。这样自定义译名或跨地区标题不会被无谓打回人工确认。
        primary_matched = bool(primary_evidence["matched"])
        alternate_matched = bool(alternate_evidence["matched"])
        if primary_matched != alternate_matched:
            return primary if primary_matched else alternate

        candidates = [
            candidate
            for candidate in (
                self._explicit_result_candidate(primary, primary_evidence),
                self._explicit_result_candidate(alternate, alternate_evidence),
            )
            if candidate is not None
        ]
        folder_label = str(context.folder_title or context.normalized_title or "").strip()
        expected_year = str(
            context.folder_year or context.filename_year or ""
        ).strip()
        if not candidates:
            error = f"显式 TMDB 标记 {tmdb_id} 在电影和剧集库中均未查询到详情"
            status = "request_error"
        else:
            source_label = f"「{folder_label}」" if folder_label else "当前来源"
            year_label = f"（{expected_year}）" if expected_year else ""
            error = (
                f"显式 TMDB 标记 {tmdb_id} 的电影/剧集详情与"
                f"{source_label}{year_label}不一致，需人工确认类型"
            )
            status = "low_confidence"
        return MatchResult(
            tmdb_id=str(tmdb_id or ""), external_id=str(tmdb_id or ""),
            provider="tmdb", media_type=primary_type, confidence=0.0,
            candidates=candidates, locked=False, need_confirm=True,
            error=error, status=status, matched_by="tmdb_id_type_check",
            threshold=1.0,
        )

    # ===== 文件名解析与清洗 =====
    def _parse_filename_fields(self, filename: str) -> dict[str, object]:
        name = filename.rsplit(".", 1)[0] if "." in filename else filename
        # 文件名中的显式 TMDB 标记优先级最高。
        explicit_tmdb_id = _explicit_tmdb_id_from_path(filename)
        if explicit_tmdb_id:
            markerless_name = _strip_explicit_tmdb_markers(name)
            episode = _extract_episode(markerless_name)
            season = _extract_season(
                markerless_name, episode_context=episode is not None
            )
            special_episode = _extract_special_episode(markerless_name)
            if special_episode is not None:
                season, episode = 0, special_episode
            guessed_type = "tv" if season is not None or episode is not None else "movie"
            return {
                "title": "", "year": "", "type": guessed_type, "tmdb_id": explicit_tmdb_id,
                "season": season, "episode": episode,
            }
        try:
            info = _guessit_info(name)
            title = str(info.get("title", "") or "")
            season = _season_number(info.get("season"))
            episode = _position_number(info.get("episode"))
            if _SEASON_RANGE_TOKEN.search(name):
                season = None
            if _has_unaccepted_release_x_position(name):
                # 与统一识别上下文保持一致：画面比例不是季集位置。
                season = None
                episode = None
            explicit_episode = _extract_episode(name)
            untrusted_guessed_episode = _guessit_episode_is_untrusted(
                name,
                explicit_episode,
                episode,
                season,
            )
            if untrusted_guessed_episode:
                episode = None
                season = None
            explicit_season = _extract_explicit_season(
                name, episode_context=(explicit_episode is not None or episode is not None),
            )
            implicit_season, implicit_season_span = _implicit_season_hint(
                name, episode_context=(explicit_episode is not None or episode is not None),
            )
            deterministic_season = (
                explicit_season if explicit_season is not None else implicit_season
            )
            special_episode = _extract_special_episode(name)
            if deterministic_season is not None:
                season = deterministic_season
            if explicit_episode is not None:
                episode = explicit_episode
            if special_episode is not None:
                season = 0
                episode = special_episode
            title_source = _strip_known_episode_suffix(name, episode, season)
            if explicit_season is None and implicit_season is not None:
                title_source = _remove_text_span(title_source, implicit_season_span)
            canonical_title, _ = _clean_release_stem(title_source)
            release_candidates, _ = _non_destructive_release_title_candidates(
                title_source
            )
            if release_candidates:
                canonical_title = release_candidates[0]
            if deterministic_season is not None:
                canonical_title = _strip_season_tokens(canonical_title)
            canonical_title = re.sub(r"\s+", " ", canonical_title).strip(" ._-")
            # GuessIt 主要提供季集等结构字段。标题以原始发布名清洗结果为锚，
            # 避免长中文标题被错误切成末尾短语（例如仅剩“S 級”）。
            if canonical_title and (
                not title
                or implicit_season is not None
                or _low_information_query(title)
                or (
                    len(_comparison_key(canonical_title).replace(" ", ""))
                    > len(_comparison_key(title).replace(" ", ""))
                    and _title_similarity_score(canonical_title, title) < 0.9
                )
            ):
                title = canonical_title
            if deterministic_season is not None:
                title = _strip_season_tokens(title)
            if special_episode is not None:
                title = _SPECIAL_EPISODE_TOKEN.sub(" ", title)
                title = strip_special_media_markers(title)
            title = re.sub(r"\s+", " ", title).strip(" ._-")
            mtype = "tv" if (
                (info.get("type") == "episode" and not untrusted_guessed_episode)
                or season is not None
                or episode is not None
            ) else "movie"
            return {
                "title": title,
                "year": str(info.get("year", "") or ""),
                "type": mtype,
                "season": season,
                "episode": episode,
            }
        except Exception as e:
            logger.warning("guessit 解析失败 type=%s", type(e).__name__)
            return {"title": name, "year": "", "type": "movie",
                    "season": None, "episode": None}

    @staticmethod
    def _release_parse_tokens(context: RecognitionContext) -> tuple[ReleaseParseToken, ...]:
        tokens: list[ReleaseParseToken] = []
        for kind, values in context.cleaned_components.items():
            for value in values:
                text = str(value or "").strip()
                if text:
                    tokens.append(ReleaseParseToken(kind=kind, value=text))
        if context.filename_year:
            tokens.append(ReleaseParseToken("year", context.filename_year))
        if context.season is not None:
            tokens.append(ReleaseParseToken("season", str(context.season)))
        if context.episode is not None:
            tokens.append(ReleaseParseToken("episode", str(context.episode)))
        return tuple(tokens)

    @staticmethod
    def _release_parse_evidence(
        context: RecognitionContext, processed, fields: dict[str, object],
    ) -> tuple[ReleaseParseEvidence, ...]:
        evidence: list[ReleaseParseEvidence] = []
        if context.filename_title:
            evidence.append(ReleaseParseEvidence(
                "title", "filename", context.filename_title, 1.0,
            ))
        if context.folder_title:
            evidence.append(ReleaseParseEvidence(
                "title", "parent_directory", context.folder_title, 0.92,
            ))
        if context.filename_year:
            evidence.append(ReleaseParseEvidence(
                "year", "filename", context.filename_year, 1.0,
            ))
        if context.folder_year:
            evidence.append(ReleaseParseEvidence(
                "year", "parent_directory", context.folder_year, 0.95,
            ))
        if context.season is not None:
            evidence.append(ReleaseParseEvidence(
                "season", "release_context", context.season, 1.0,
            ))
        if context.episode is not None:
            evidence.append(ReleaseParseEvidence(
                "episode", "release_context", context.episode, 1.0,
            ))
        for rule in list(getattr(processed, "applied_rules", None) or []):
            evidence.append(ReleaseParseEvidence(
                "preprocess_rule", "recognition_preprocess",
                str(rule.get("name") or rule.get("action") or "rule"), 1.0,
            ))
        explicit_id = str(fields.get("tmdb_id") or "").strip()
        if explicit_id:
            evidence.append(ReleaseParseEvidence(
                "tmdb_id", "explicit_marker", explicit_id, 1.0,
            ))
        return tuple(evidence)

    def parse_media(
        self, filename: str, parent_path: str = "", match: MatchResult | None = None,
    ) -> ReleaseParseResult:
        """一次性生成标题、原始季集、有效季集与可审计证据。"""
        fields = dict(self._parse_filename_fields(filename) or {})
        context = extract_recognition_context(filename, parent_path)
        from app.modules.recognition_preprocess_rules import apply_rules

        processed = apply_rules(
            filename, parent_path, season=context.season, episode=context.episode,
        )
        effective_season = processed.season
        effective_episode = processed.episode
        if match is not None and match.preprocess_evaluated:
            effective_season = match.effective_season
            effective_episode = match.effective_episode
        mapping = dict((getattr(match, "metadata", None) or {}).get("episode_mapping") or {})
        if (
            mapping.get("changed")
            and mapping.get("source_season") == effective_season
            and mapping.get("source_episode") == effective_episode
        ):
            effective_season = mapping.get("target_season")
            effective_episode = mapping.get("target_episode")
        if match is not None and match.season_override is not None:
            effective_season = match.season_override

        result = ReleaseParseResult(
            filename=str(filename or ""),
            parent_path=str(parent_path or ""),
            title=str(fields.get("title") or context.normalized_title or ""),
            year=str(fields.get("year") or context.filename_year or context.folder_year or ""),
            media_type=str(fields.get("type") or context.media_type or "movie"),
            tmdb_id=str(fields.get("tmdb_id") or ""),
            source_season=context.season,
            source_episode=context.episode,
            effective_season=effective_season,
            effective_episode=effective_episode,
            context=context,
            preprocess_rules=tuple(
                dict(item) for item in list(processed.applied_rules or [])
            ),
            tokens=self._release_parse_tokens(context),
            evidence=self._release_parse_evidence(context, processed, fields),
        )
        if match is not None:
            match.metadata = {
                **dict(match.metadata or {}),
                "release_parse": result.diagnostic_dict(),
            }
        return result

    def parse_source_position(self, filename: str, parent_path: str = "") -> tuple[int | None, int | None]:
        """返回未应用预处理和集数映射的原始季集位置。"""
        context = extract_recognition_context(filename, parent_path)
        return context.season, context.episode

    def prepare_recognition(self, filename: str, parent_path: str = ""):
        """构造统一识别投影；原始显式 ID、人工锁和强制规则仍在其前判定。"""
        from app.modules.recognition_preprocess_rules import apply_rules

        raw_context = extract_recognition_context(filename, parent_path)
        return apply_rules(
            filename, parent_path, season=raw_context.season, episode=raw_context.episode,
        )

    @staticmethod
    def _attach_preprocess(result: MatchResult, processed) -> MatchResult:
        result.preprocess_evaluated = True
        result.recognition_filename = processed.filename
        result.recognition_parent_path = processed.parent_path
        result.effective_season = processed.season
        result.effective_episode = processed.episode
        result.preprocess_rules = list(processed.applied_rules)
        mapping = dict((result.metadata or {}).get("episode_mapping") or {})
        if (
            mapping.get("changed")
            and mapping.get("source_season") == processed.season
            and mapping.get("source_episode") == processed.episode
        ):
            result.effective_season = mapping.get("target_season")
            result.effective_episode = mapping.get("target_episode")
        if result.season_override is not None:
            result.effective_season = result.season_override
        return result

    def clean_title(self, title: str) -> str:
        """生成搜索词；只删除已知发布噪声，保留正式括号副标题。"""
        if not title:
            return ""
        raw = str(title or "").strip()
        stem = raw.rsplit(".", 1)[0] if re.search(r"\.[A-Za-z0-9]{2,5}$", raw) else raw
        guessed = _guessit_info(stem)
        episode = _extract_episode(stem)
        if episode is None:
            guessed_episode = _position_number(guessed.get("episode"))
            guessed_season = _season_number(guessed.get("season"))
            if not _guessit_episode_is_untrusted(
                stem,
                episode,
                guessed_episode,
                guessed_season,
            ):
                episode = guessed_episode
        explicit_season = _extract_explicit_season(
            stem, episode_context=episode is not None,
        )
        implicit_season, implicit_season_span = _implicit_season_hint(
            stem, episode_context=episode is not None,
        )
        season = explicit_season if explicit_season is not None else implicit_season
        title_source = _strip_known_episode_suffix(stem, episode, season)
        if (
            explicit_season is None
            and implicit_season is not None
        ):
            title_source = _remove_text_span(title_source, implicit_season_span)
        cleaned, _ = _clean_release_stem(title_source)
        release_candidates, _ = _non_destructive_release_title_candidates(
            title_source
        )
        if release_candidates:
            cleaned = release_candidates[0]
        guessed_title = re.sub(
            r"\s+", " ", _strip_season_tokens(str(guessed.get("title") or ""))
        ).strip(" ._-")
        if not cleaned and guessed_title:
            cleaned = guessed_title
        elif _prefer_guessit_numeric_title(
            guessed_title, cleaned, guessed.get("year")
        ):
            cleaned = guessed_title
        cleaned = _strip_season_tokens(cleaned)
        return re.sub(r"\s+", " ", cleaned).strip(" ._-")

    @staticmethod
    def parse_resource_tags(filename: str) -> dict[str, str]:
        """仅从文件名中提取可验证的发布规格，不猜测缺失信息。"""
        text = str(filename or "")

        def first(patterns: tuple[tuple[str, str], ...]) -> str:
            for pattern, label in patterns:
                if re.search(pattern, text, re.IGNORECASE):
                    return label
            return ""

        resolution = first((
            (r"(?:^|[._ -])(2160p|4k|uhd)(?:[._ -]|$)", "2160p"),
            (r"(?:^|[._ -])1080p(?:[._ -]|$)", "1080p"),
            (r"(?:^|[._ -])720p(?:[._ -]|$)", "720p"),
            (r"(?:^|[._ -])(?:480p|576p)(?:[._ -]|$)", "SD"),
        ))
        source = first((
            (r"(?:^|[._ -])itunes(?:[._ -]|$)", "iTunes"),
            (r"(?:^|[._ -])(?:netflix|nf)(?:[._ -]|$)", "Netflix"),
            (r"(?:^|[._ -])(?:amazon|amzn)(?:[._ -]|$)", "Amazon"),
            (r"(?:^|[._ -])(?:disney|dsnp)(?:[._ -]|$)", "Disney+"),
            (r"(?:^|[._ -])(?:atvp|apple)[ ._-]?tv(?:[._ -]|$)", "Apple TV+"),
            (r"(?:^|[._ -])hbo(?:[._ -]|$)", "HBO"),
        ))
        media = first((
            (r"web[ ._-]?dl", "WEB-DL"),
            (r"web[ ._-]?rip", "WEBRip"),
            (r"blu[ ._-]?ray", "BluRay"),
            (r"(?:^|[._ -])remux(?:[._ -]|$)", "Remux"),
            (r"(?:^|[._ -])hdtv(?:[._ -]|$)", "HDTV"),
        ))
        video_codec = first((
            (r"(?:h[ ._-]?265|hevc|x265)", "H.265"),
            (r"(?:h[ ._-]?264|avc|x264)", "H.264"),
            (r"(?:^|[._ -])av1(?:[._ -]|$)", "AV1"),
        ))
        effects = []
        for pattern, label in (
            (r"hdr10\+|hdr10plus", "HDR10+"),
            (r"(?:^|[._ -])hdr10(?:[._ -]|$)", "HDR10"),
            (r"(?:dovi|dolby[ ._-]?vision)", "DoVi"),
            (r"(?:^|[._ -])hdr(?:[._ -]|$)", "HDR"),
        ):
            if re.search(pattern, text, re.IGNORECASE) and label not in effects:
                effects.append(label)
        audio = []
        for pattern, label in (
            (r"(?:ddp|eac3)[ ._-]?7[ ._-]?1", "DDP 7.1"),
            (r"(?:ddp|eac3)[ ._-]?5[ ._-]?1", "DDP 5.1"),
            (r"(?:^|[._ -])(?:ddp|eac3)(?:[._ -]|$)", "DDP"),
            (r"(?:^|[._ -])atmos(?:[._ -]|$)", "Atmos"),
            (r"truehd", "TrueHD"),
            (r"dts[ ._-]?(?:hd|ma)", "DTS-HD MA"),
            (r"(?:^|[._ -])aac(?:[._ -]|$)", "AAC"),
        ):
            if re.search(pattern, text, re.IGNORECASE) and label not in audio:
                audio.append(label)
        stem = text.rsplit(".", 1)[0]
        release_group_match = _tail_release_group(stem)
        release_group = release_group_match[0] if release_group_match else ""
        return {
            "resolution": resolution,
            "source": source,
            "media": media,
            "effect": " · ".join(effects),
            "video_codec": video_codec,
            "audio": " · ".join(audio),
            "release_group": release_group,
        }

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        if not a or not b:
            return 0.0
        al, bl = a.lower(), b.lower()
        if al == bl:
            return 1.0
        if al in bl or bl in al:
            return 0.8  # 包含关系降权，避免 Inception 误匹配 Inception: Music
        aw, bw = set(al.split()), set(bl.split())
        if not aw or not bw:
            return 0.0
        return len(aw & bw) / len(aw | bw)

    @staticmethod
    def _compact_latin(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", (value or "").lower())

    @classmethod
    def _pinyin_similarity(cls, query: str, candidate: str) -> float:
        """拼音只做中文候选的严格辅助匹配，短词和非拉丁查询不启用。"""
        query_key = cls._compact_latin(query)
        if len(query_key) < 4 or not re.fullmatch(r"[a-z0-9]+", query_key):
            return 0.0
        if not re.search(r"[\u4e00-\u9fff]", candidate or ""):
            return 0.0
        try:
            from pypinyin import Style, lazy_pinyin
        except ImportError:
            return 0.0
        full = cls._compact_latin("".join(lazy_pinyin(candidate, style=Style.NORMAL)))
        if not full:
            return 0.0
        if query_key == full:
            return 1.0
        # 只接受接近完整标题的包含关系，避免短拼音误撞长中文标题。
        shorter, longer = sorted((query_key, full), key=len)
        if shorter in longer and len(shorter) / len(longer) >= 0.85:
            return 0.88
        return 0.0

    @classmethod
    def _title_similarity(cls, query: str, candidate: str) -> float:
        return max(cls._similarity(query, candidate), cls._pinyin_similarity(query, candidate))

    def _enrich_candidate_for_scoring(
        self, candidate: dict, fallback_media_type: str
    ) -> tuple[dict, bool]:
        """补齐候选别名，并报告详情请求是否完成且可验证。"""
        tmdb_id = str(candidate.get("id") or "").strip()
        media_type = str(candidate.get("media_type") or fallback_media_type)
        media_type = "tv" if media_type == "tv" else "movie"
        if not tmdb_id:
            return candidate, False
        key = (media_type, tmdb_id)
        cached, detail, detail_failed = self._cached_recognition_detail(key)
        if not cached:
            try:
                detail_method = getattr(
                    self.client, "detail_with_alternative_titles", None
                )
                if callable(detail_method):
                    raw_detail = detail_method(tmdb_id, media_type)
                else:
                    raw_detail = self.client.detail(tmdb_id, media_type)
                detail_failed = not isinstance(raw_detail, dict)
                detail = dict(raw_detail) if isinstance(raw_detail, dict) else {}
                self._remember_recognition_detail(
                    key, detail, failed=detail_failed
                )
                self._remember_detail(self._detail_cache, key, detail)
            except Exception as exc:
                logger.warning(
                    "TMDB 候选别名补全失败 tmdb=%s type=%s error=%s",
                    tmdb_id, media_type, type(exc).__name__,
                )
                detail = {}
                detail_failed = True
                self._remember_recognition_detail(key, detail, failed=True)
        if not detail:
            # ``{}`` 可以是客户端成功返回的空对象；它代表没有补充别名，
            # 但覆盖请求已完成。只有异常或非字典响应才是不完整覆盖。
            return candidate, not detail_failed
        merged = dict(candidate)
        for field_name in (
            "name", "title", "original_name", "original_title",
            "first_air_date", "release_date", "overview", "poster_path",
            "backdrop_path",
        ):
            if not merged.get(field_name) and detail.get(field_name):
                merged[field_name] = detail[field_name]
        for alias_field in ("alternative_titles", "translations"):
            if detail.get(alias_field):
                merged[alias_field] = detail[alias_field]
        return merged, not detail_failed

    def _attach_unique_target_season_year_evidence(
        self,
        *,
        context: RecognitionContext,
        expected_year: str,
        scored: list[tuple[CandidateScoreBreakdown, dict]],
    ) -> bool:
        """为唯一强标题 TV 候选附加目标季首播年证据。

        该证据只移除“系列首播年与文件年份不一致”的惩罚，不直接降低全局
        90% 阈值。后续仍须通过低分强证据、候选间距、歧义门禁、identity
        映射以及 Organizer 的最终季集复核，才能进入自动整理。
        """
        expected = str(expected_year or "").strip()
        if (
            context.media_type != "tv"
            or context.season is None
            or context.season < 1
            or context.episode is None
            or context.episode < 1
            or not re.fullmatch(r"(?:19|20)\d{2}", expected)
        ):
            return False

        eligible: list[tuple[CandidateScoreBreakdown, dict]] = []
        for breakdown, raw in scored:
            strong_title_score = max(
                breakdown.title_score,
                breakdown.original_title_score,
                breakdown.alias_score,
            )
            if (
                not breakdown.rejected_constraints
                and strong_title_score >= _VERIFIED_AUTOMATIC_IDENTITY_MIN_CONFIDENCE
                and str(raw.get("media_type") or "").strip().lower() == "tv"
                and str(raw.get("id") or "").strip()
            ):
                eligible.append((breakdown, raw))

        selected_pair: tuple[CandidateScoreBreakdown, dict] | None = None
        if len(eligible) == 1:
            selected_pair = eligible[0]
        elif len(eligible) > 1:
            # 模糊相似标题（例如作品名后追加 ``Abridged``）不应阻断一个
            # 与 TMDB 主标题、原始标题或官方别名完全一致的候选。这里仍然
            # 失败关闭：只能有一个精确官方标题锚点，且它在现有评分上至少
            # 达到低分强证明地板并显著领先第二候选。若两个候选复用同一
            # 官方别名，则继续交给歧义门禁/人工确认，不能借年份强行挑选。
            exact = [
                item for item in eligible
                if max(
                    item[0].title_score,
                    item[0].original_title_score,
                    item[0].alias_score,
                ) >= 0.999
            ]
            if len(exact) == 1:
                selected_score = float(exact[0][0].final_score)
                runner_score = max(
                    float(item[0].final_score)
                    for item in eligible
                    if item is not exact[0]
                )
                if (
                    selected_score >= _VERIFIED_AUTOMATIC_IDENTITY_MIN_CONFIDENCE
                    and selected_score - runner_score
                    >= _VERIFIED_AUTOMATIC_IDENTITY_MIN_CANDIDATE_GAP
                ):
                    selected_pair = exact[0]
        # 目标季年份不能替多个精确同名候选做消歧。
        if selected_pair is None:
            return False

        _, selected = selected_pair
        candidate_date = str(
            selected.get("first_air_date") or selected.get("release_date") or ""
        ).strip()
        if candidate_date[:4] == expected:
            return False
        tmdb_id = str(selected.get("id") or "").strip()
        detail = self.get_detail(tmdb_id, "tv")
        if not isinstance(detail, dict) or not detail:
            return False
        mapping = infer_episode_mapping(
            source_season=context.season,
            source_episode=context.episode,
            parent_path=context.parent_path,
            detail=detail,
            mode="auto",
        )
        evidence = _build_target_season_year_evidence(
            detail=detail,
            tmdb_id=tmdb_id,
            context=context,
            expected_year=expected,
            mapping=mapping,
        )
        if evidence is None:
            return False
        selected["_verified_target_season_year"] = evidence
        return True

    def _resolve_ambiguous_alias_by_position(
        self,
        *,
        context: RecognitionContext,
        scored: list[tuple[CandidateScoreBreakdown, dict]],
        ambiguous_aliases: list[str],
        threshold: float,
    ) -> tuple[str, EpisodeMappingPlan, dict[str, object]] | None:
        """用 TMDB 季集位置对精确同名候选做一次严格、失败关闭的消歧。

        该步骤不降低标题阈值，也不把网络失败当作候选淘汰证据。只有一个
        候选的季集位置被 TMDB 明确验证通过，且其余同名候选均被明确证明为
        ``季不存在`` 或 ``集号越界`` 时才自动选择；任何详情缺失、集数未知
        或仍有多个候选可容纳该集号，都会保留人工确认。

        对 ``S01E100+`` 或 ``Title - 100+`` 这类长篇动画发布编号，仅在
        精确同名候选消歧内部额外尝试绝对集数映射。它不能作为普通候选的
        全局放宽规则；裸集号映射仍保留 ``source_season=None``，供后续证明
        与 Organizer 复核原始输入语义。
        """
        if (
            context.media_type != "tv"
            or context.episode is None
            or not ambiguous_aliases
        ):
            return None
        bare_absolute_episode = bool(
            context.season is None and context.episode >= 100
        )
        if context.season is None and not bare_absolute_episode:
            return None

        ambiguous_keys = {
            _comparison_key(value) for value in ambiguous_aliases if value
        }
        exact_candidates: list[tuple[CandidateScoreBreakdown, dict]] = []
        for breakdown, candidate in scored:
            if breakdown.rejected_constraints or breakdown.final_score < threshold:
                continue
            candidate_id = str(candidate.get("id") or "").strip()
            media_type = str(candidate.get("media_type") or context.media_type)
            if not candidate_id or media_type != "tv":
                continue
            alias_keys = {
                _comparison_key(alias)
                for alias in _candidate_identity_labels(candidate)
                if alias
            }
            if ambiguous_keys.intersection(alias_keys):
                exact_candidates.append((breakdown, candidate))

        if not (2 <= len(exact_candidates) <= MAX_ALIAS_VALIDATION_CANDIDATES):
            return None

        passed: list[tuple[str, EpisodeMappingPlan]] = []
        excluded: list[dict[str, object]] = []
        hard_fail_reasons = {"season_not_found", "episode_out_of_range"}
        hard_year_fail_reasons = {
            "year_mismatch",
            "target_season_year_mismatch",
            "target_season_not_found",
        }
        source_year = str(context.filename_year or context.folder_year or "").strip()
        mapping_source_season = 1 if bare_absolute_episode else context.season
        allow_absolute_probe = (
            mapping_source_season == 1 and context.episode >= 100
        )

        def preserve_source_position(mapping: EpisodeMappingPlan) -> EpisodeMappingPlan:
            if not bare_absolute_episode:
                return mapping
            # ``infer_episode_mapping`` 需要临时以 S01 计算绝对位置，但下游
            # _attach_preprocess 只会消费“源位置仍等于原始解析位置”的映射。
            # 因此证明中必须保留裸集号的 source_season=None，而不是伪造 S01。
            return replace(mapping, source_season=None)

        for _breakdown, candidate in exact_candidates:
            candidate_id = str(candidate.get("id") or "").strip()
            detail = self.get_detail(candidate_id, "tv")
            if not detail:
                # 网络失败、空详情或缓存中只有失败结果均不能作为排除证据。
                return None
            detail_id = str(detail.get("id") or "").strip()
            if detail_id and detail_id != candidate_id:
                # 详情身份不一致绝不能被当作候选淘汰证据。
                return None
            # 搜索响应通常带作品首播年，详情响应在测试替身或部分兼容 API
            # 中可能只返回 seasons/alternative_titles。合并后再做硬校验，既不
            # 丢失来源年份证据，也不把缺字段误判为年份冲突。
            verified_detail = dict(candidate)
            verified_detail.update(detail)

            mapping = infer_episode_mapping(
                source_season=mapping_source_season,
                source_episode=context.episode,
                parent_path=context.parent_path,
                detail=verified_detail,
                mode="auto",
            )
            validation = _validate_tmdb_position(
                verified_detail, "tv", mapping.target_season, mapping.target_episode,
            )

            if not validation.get("passed") and allow_absolute_probe:
                absolute_mapping = infer_episode_mapping(
                    source_season=mapping_source_season,
                    source_episode=context.episode,
                    parent_path=context.parent_path,
                    detail=verified_detail,
                    mode="absolute",
                )
                absolute_validation = _validate_tmdb_position(
                    verified_detail,
                    "tv",
                    absolute_mapping.target_season,
                    absolute_mapping.target_episode,
                )
                if absolute_validation.get("passed"):
                    mapping = absolute_mapping
                    validation = absolute_validation

            if (
                not validation.get("passed")
                and mapping_source_season == 2
            ):
                counts = season_episode_counts(verified_detail)
                # 发布方把分割放送写成 S02，而 TMDB 可能仍保留为唯一的
                # Season 01。只有逐集播出日期完整、断档边界唯一可证明时才
                # 映射；网络失败或详情缺失属于证据不足，不能借此淘汰候选。
                if len(counts) == 1 and mapping_source_season not in counts:
                    target_season, declared_count = next(iter(counts.items()))
                    season_detail = self.get_tv_season_detail(
                        candidate_id, target_season,
                    )
                    episodes = (
                        season_detail.get("episodes")
                        if isinstance(season_detail, dict)
                        else None
                    )
                    if not isinstance(episodes, list) or len(episodes) != declared_count:
                        return None
                    if any(
                        not isinstance(item, dict)
                        or not str(item.get("air_date") or "").strip()
                        or item.get("episode_number") is None
                        for item in episodes
                    ):
                        return None
                    cour_mapping = infer_merged_season_cour_mapping(
                        source_season=mapping_source_season,
                        source_episode=context.episode,
                        detail=verified_detail,
                        season_detail=season_detail,
                    )
                    cour_validation = _validate_tmdb_position(
                        verified_detail,
                        "tv",
                        cour_mapping.target_season,
                        cour_mapping.target_episode,
                    )
                    if cour_mapping.confidence >= 1.0 and cour_validation.get("passed"):
                        mapping = cour_mapping
                        validation = cour_validation

            if validation.get("passed"):
                year_matches, year_reason = _source_year_matches_tmdb(
                    verified_detail,
                    "tv",
                    source_year,
                    target_season=mapping.target_season,
                )
                if not year_matches and year_reason == "target_season_year_mismatch":
                    # 跨年季与 split-cour 中季首播年可能早于具体集播出年。
                    # 只在季年份已经不匹配时才补取季详情，不增加常规请求量。
                    year_matches, year_reason = _source_year_matches_tmdb(
                        verified_detail,
                        "tv",
                        source_year,
                        target_season=mapping.target_season,
                        target_episode=mapping.target_episode,
                        season_episodes=self._season_episodes(
                            candidate_id, mapping.target_season,
                        ),
                    )
                if year_matches:
                    passed.append((candidate_id, preserve_source_position(mapping)))
                    continue
                if year_reason not in hard_year_fail_reasons:
                    # 缺少季年份等同于证据不足，不得借此淘汰候选。
                    return None
                excluded.append({
                    "tmdb_id": candidate_id,
                    "reason": "year_mismatch",
                    "year_reason": year_reason,
                    "source_year": source_year,
                    "season": mapping.target_season,
                    "episode": mapping.target_episode,
                })
                continue

            reason = str(validation.get("reason") or "position_unverified")
            if reason not in hard_fail_reasons:
                # seasons_missing / episode_count_missing 等均属于证据不足。
                return None
            excluded.append({
                "tmdb_id": candidate_id,
                "reason": reason,
                "season": validation.get("season"),
                "episode": validation.get("episode"),
            })

        if len(passed) != 1 or len(excluded) != len(exact_candidates) - 1:
            return None

        selected_id, selected_mapping = passed[0]
        diagnostic: dict[str, object] = {
            "selected_tmdb_id": selected_id,
            "source_season": context.season,
            "source_episode": context.episode,
            "candidate_count": len(exact_candidates),
            "excluded": excluded,
        }
        return selected_id, selected_mapping, diagnostic

    def _recognize_context(
        self,
        context: RecognitionContext,
        filename: str,
        *,
        allow_lock: bool,
        matched_by: str,
        match_mode: str | None = None,
    ) -> RecognitionResult:
        queries = generate_query_variants(context)
        effective_match_mode = self.match_mode if match_mode is None else match_mode
        threshold = 0.9 if effective_match_mode == "strict" else 0.6
        if not queries:
            return RecognitionResult(
                media_type=context.media_type, need_confirm=True, error="无法解析标题",
                status="config_error", matched_by=matched_by, threshold=threshold,
                context=context, query_variants=[],
                threshold_decision=decide_threshold(0.0, threshold),
            )

        expected_year = context.filename_year or context.folder_year
        raw_candidates: dict[tuple[str, str], dict] = {}
        search_only_queries: list[str] = []
        search_only_variant_used = False
        type_mismatch_seen = False
        search_attempts: list[dict[str, object]] = []

        def collect(
            year: str,
            query_pool: list[str] | None = None,
            *,
            search_only: bool = False,
        ) -> None:
            nonlocal type_mismatch_seen
            requested_type = str(context.media_type or "").strip().lower()
            for query in list(query_pool or queries):
                self._clear_thread_search_outcome()
                results = self.search(query, year, context.media_type)
                outcome = self._take_thread_search_outcome(results)
                search_attempts.append({
                    "query": str(query or ""),
                    "year": str(year or ""),
                    "status": outcome.status,
                    "error": outcome.error,
                    "cache_hit": outcome.cache_hit,
                    "empty_cache_hit": outcome.empty_cache_hit,
                    "search_only": bool(search_only),
                    "result_count": len(outcome.results),
                })
                for candidate in results:
                    tmdb_id = str(candidate.get("id") or "").strip()
                    reported_type = str(candidate.get("media_type") or "").strip().lower()
                    # 类型专用搜索端点可能省略 media_type；此时应继承请求上下文，
                    # 否则后续“目标季首播年”与季集验证会把真实 TV 候选误当成
                    # 无类型候选。若服务端明确返回了相反类型，则失败关闭。
                    if reported_type and requested_type and reported_type != requested_type:
                        type_mismatch_seen = True
                        continue
                    media_type = reported_type or requested_type
                    if media_type not in {"movie", "tv"}:
                        continue
                    key = (media_type, tmdb_id)
                    if tmdb_id and key not in raw_candidates:
                        raw = dict(candidate)
                        raw["media_type"] = media_type
                        if search_only:
                            raw["_search_only_variant"] = True
                            raw["_search_only_query"] = query
                        raw_candidates[key] = raw

        collect(expected_year)
        if not raw_candidates and expected_year:
            collect("")
        if not raw_candidates:
            search_only_queries = generate_search_only_query_variants(context, queries)
            if search_only_queries:
                collect(expected_year, search_only_queries, search_only=True)
                if not raw_candidates and expected_year:
                    collect("", search_only_queries, search_only=True)
                search_only_variant_used = bool(raw_candidates)

        if not raw_candidates:
            failed_attempt = next((
                item for item in search_attempts
                if item.get("status") in {"config_error", "request_error"}
            ), None)
            if failed_attempt is not None:
                status = str(failed_attempt.get("status") or "request_error")
                error = str(failed_attempt.get("error") or "TMDB 搜索失败")
            elif type_mismatch_seen and any(
                item.get("status") == "matched" for item in search_attempts
            ):
                error = "TMDB 搜索结果媒体类型与请求不一致"
                status = "no_result"
            else:
                error = "TMDB 无搜索结果"
                status = "no_result"
            return RecognitionResult(
                media_type=context.media_type, need_confirm=True, error=error,
                status=status, matched_by=matched_by, threshold=threshold,
                context=context, query_variants=queries,
                threshold_decision=decide_threshold(0.0, threshold),
                metadata={"search_attempts": [dict(item) for item in search_attempts]},
            )

        animation_marker = _explicit_animation_source_marker(context)
        animation_filtered_count = 0
        initial_scores = [
            score_candidate(context, raw) for raw in raw_candidates.values()
        ]
        target_season_year_context = bool(
            expected_year
            and context.media_type == "tv"
            and context.season is not None
            and context.season >= 1
            and context.episode is not None
            and context.episode >= 1
        )
        if (
            target_season_year_context
            and raw_candidates
            and max(item.final_score for item in initial_scores) < threshold
        ):
            # 年份筛选可能只返回“当年新开播”的错误条目，而真正的第 N 季
            # 所属系列首播更早。仅在显式 SxxExx 且当前候选均未达阈值时，
            # 再用原始查询补一次无年份搜索；结果仍进入完整歧义门禁。
            collect("")

        if animation_marker:
            animation_candidates = {
                key: raw
                for key, raw in raw_candidates.items()
                if _TMDB_ANIMATION_GENRE_ID in _tmdb_genre_ids(raw)
            }
            if animation_candidates:
                animation_filtered_count = (
                    len(raw_candidates) - len(animation_candidates)
                )
                raw_candidates = animation_candidates

        initial_scores = [
            score_candidate(context, raw) for raw in raw_candidates.values()
        ]
        needs_alias_validation = (
            len(raw_candidates) >= 2
            and any(_eligible_latin_alias_query(query) for query in queries)
        )
        alias_validation_overflow = False
        alias_validation_incomplete = False
        if initial_scores and (
            max(item.final_score for item in initial_scores) < threshold
            or needs_alias_validation
        ):
            # 普通低分识别只补全 Top 3；拉丁/Romaji 歧义检查则覆盖
            # 有界的全部候选，避免第 4 个及以后候选复用同一别名却漏检。
            enrich_limit = 3
            if needs_alias_validation:
                enrich_limit = MAX_ALIAS_ENRICHMENT_CANDIDATES
                alias_validation_overflow = len(raw_candidates) > enrich_limit
                alias_validation_incomplete = alias_validation_overflow
            for key in list(raw_candidates)[:enrich_limit]:
                enriched, detail_available = self._enrich_candidate_for_scoring(
                    raw_candidates[key], context.media_type
                )
                raw_candidates[key] = enriched
                if needs_alias_validation and not detail_available:
                    alias_validation_incomplete = True

        scored: list[tuple[CandidateScoreBreakdown, dict]] = []
        rejected_constraints: list[str] = []
        for raw in raw_candidates.values():
            breakdown = score_candidate(context, raw)
            scored.append((breakdown, raw))
            for reason in breakdown.rejected_constraints:
                if reason not in rejected_constraints:
                    rejected_constraints.append(reason)
        scored.sort(
            key=lambda item: (
                -item[0].final_score,
                str(item[1].get("name") or item[1].get("title") or "").lower(),
                str(item[1].get("id") or ""),
            )
        )

        if (
            not search_only_variant_used
            and self._attach_unique_target_season_year_evidence(
                context=context,
                expected_year=expected_year,
                scored=scored,
            )
        ):
            scored = [
                (score_candidate(context, raw), raw)
                for _, raw in scored
            ]
            scored.sort(
                key=lambda item: (
                    -item[0].final_score,
                    str(item[1].get("name") or item[1].get("title") or "").lower(),
                    str(item[1].get("id") or ""),
                )
            )

        ambiguous_aliases = _ambiguous_exact_alias_queries(queries, scored, threshold)
        ambiguous_identities = (
            _ambiguous_exact_identity_queries(queries, scored, threshold)
            if context.media_type == "tv" and context.episode is not None
            else []
        )
        position_ambiguities = _unique_text((
            *ambiguous_aliases, *ambiguous_identities,
        ))
        alias_position_resolution: dict[str, object] | None = None
        resolved_mapping: EpisodeMappingPlan | None = None
        resolved_tmdb_id = ""
        if position_ambiguities and not alias_validation_incomplete:
            resolution = self._resolve_ambiguous_alias_by_position(
                context=context,
                scored=scored,
                ambiguous_aliases=position_ambiguities,
                threshold=threshold,
            )
            if resolution is not None:
                resolved_tmdb_id, resolved_mapping, alias_position_resolution = resolution
                # 候选展示和后续阈值判定必须消费同一个唯一消歧结果。
                scored = [
                    item for item in scored
                    if str(item[1].get("id") or "").strip() == resolved_tmdb_id
                ] + [
                    item for item in scored
                    if str(item[1].get("id") or "").strip() != resolved_tmdb_id
                ]

        candidates: list[Candidate] = []
        for breakdown, raw in scored[:3]:
            title = str(raw.get("name") or raw.get("title") or "")
            original = str(raw.get("original_name") or raw.get("original_title") or "")
            release_date = str(raw.get("first_air_date") or raw.get("release_date") or "")
            media_type = str(raw.get("media_type") or context.media_type)
            candidates.append(Candidate(
                tmdb_id=str(raw.get("id") or ""),
                title=title,
                year=release_date[:4],
                score=breakdown.final_score,
                media_type=media_type,
                original_title=original,
                overview=str(raw.get("overview") or ""),
                poster_path=str(raw.get("poster_path") or ""),
                backdrop_path=str(raw.get("backdrop_path") or ""),
                release_date=release_date,
                aliases=_candidate_aliases(raw),
                score_breakdown=breakdown,
                provider="tmdb",
                external_id=str(raw.get("id") or ""),
                metadata={"genre_ids": sorted(_tmdb_genre_ids(raw))},
            ))

        best = candidates[0]
        selected_season_year_evidence = _validated_target_season_year_evidence(
            scored[0][1], context, expected_year,
        )
        decision_constraints = list(
            best.score_breakdown.rejected_constraints if best.score_breakdown else []
        )
        best_genre_ids = _tmdb_genre_ids(scored[0][1])
        animation_verified = bool(
            not animation_marker
            or _TMDB_ANIMATION_GENRE_ID in best_genre_ids
        )
        if animation_marker and not animation_verified:
            decision_constraints.append("animation_evidence_mismatch")
            if "animation_evidence_mismatch" not in rejected_constraints:
                rejected_constraints.append("animation_evidence_mismatch")
        if search_only_variant_used:
            decision_constraints.append("search_only_variant")
            if "search_only_variant" not in rejected_constraints:
                rejected_constraints.append("search_only_variant")
        near_tie = False
        if alias_position_resolution is None and len(candidates) >= 2:
            runner_up = candidates[1]
            best_labels = {
                _comparison_key(value)
                for value in (best.title, best.original_title, *best.aliases)
                if value
            }
            runner_labels = {
                _comparison_key(value)
                for value in (runner_up.title, runner_up.original_title, *runner_up.aliases)
                if value
            }
            near_tie = bool(
                best.tmdb_id
                and runner_up.tmdb_id
                and best.tmdb_id != runner_up.tmdb_id
                and best.score >= threshold
                and runner_up.score >= threshold
                and abs(best.score - runner_up.score) <= 0.02
                and best_labels.intersection(runner_labels)
                and (not best.year or not runner_up.year or best.year == runner_up.year)
            )
        if ambiguous_aliases and alias_position_resolution is None:
            decision_constraints.append("ambiguous_romaji_alias")
            if "ambiguous_romaji_alias" not in rejected_constraints:
                rejected_constraints.append("ambiguous_romaji_alias")
        elif ambiguous_identities and alias_position_resolution is None:
            decision_constraints.append("ambiguous_exact_title")
            if "ambiguous_exact_title" not in rejected_constraints:
                rejected_constraints.append("ambiguous_exact_title")
        elif near_tie:
            decision_constraints.append("ambiguous_near_tie")
            if "ambiguous_near_tie" not in rejected_constraints:
                rejected_constraints.append("ambiguous_near_tie")
        elif alias_validation_overflow:
            decision_constraints.append("romaji_candidate_overflow")
            if "romaji_candidate_overflow" not in rejected_constraints:
                rejected_constraints.append("romaji_candidate_overflow")
        elif alias_validation_incomplete:
            decision_constraints.append("romaji_alias_coverage_incomplete")
            if "romaji_alias_coverage_incomplete" not in rejected_constraints:
                rejected_constraints.append("romaji_alias_coverage_incomplete")
        if queries and all(_low_information_query(query) for query in queries):
            decision_constraints.append("low_information_title")
            if "low_information_title" not in rejected_constraints:
                rejected_constraints.append("low_information_title")
        decision = decide_threshold(best.score, threshold, decision_constraints)
        result = RecognitionResult(
            tmdb_id=best.tmdb_id, title=best.title, year=best.year,
            media_type=best.media_type if best.media_type in {"movie", "tv"} else context.media_type,
            # `best` 来自当前 TMDB 搜索结果。身份字段必须在产生处保持一致，
            # 否则下游需要跨文件复用“已验证 TMDB 身份”时会把真实搜索结果
            # 误判为来源不明。不要在消费端把空 provider 猜成 TMDB。
            provider=str(best.provider or "tmdb"),
            external_id=str(best.external_id or best.tmdb_id or ""),
            confidence=best.score, candidates=candidates, matched_by=matched_by,
            threshold=threshold, context=context, query_variants=queries,
            threshold_decision=decision, rejected_constraints=rejected_constraints,
            metadata={
                **(
                    {"alias_position_resolution": alias_position_resolution}
                    if alias_position_resolution is not None else {}
                ),
                **(
                    {
                        "search_only_query_variants": list(search_only_queries),
                        "search_only_variant_used": True,
                    }
                    if search_only_variant_used else {}
                ),
                **(
                    {"target_season_year_evidence": selected_season_year_evidence}
                    if selected_season_year_evidence is not None else {}
                ),
                **(
                    {
                        "content_kind_evidence": {
                            "source": "explicit_donghua_marker",
                            "marker": animation_marker,
                            "required_genre_id": _TMDB_ANIMATION_GENRE_ID,
                            "candidate_genre_ids": sorted(best_genre_ids),
                            "verified": animation_verified,
                            "filtered_non_animation_candidates": animation_filtered_count,
                        }
                    }
                    if animation_marker else {}
                ),
                "recognition_evidence": {
                    "matched_query": str(
                        best.score_breakdown.matched_query
                        if best.score_breakdown else ""
                    ),
                    "matched_title": str(
                        best.score_breakdown.matched_title
                        if best.score_breakdown else ""
                    ),
                    "filename_title": str(context.filename_title or ""),
                    "folder_title": str(context.folder_title or ""),
                },
            },
        )
        proof_precheck = _verified_automatic_identity_precheck(
            result=result,
            context=context,
            scored=scored,
            decision_constraints=decision_constraints,
            expected_year=expected_year,
        )
        if decision["passed"] or proof_precheck:
            position_required = result.media_type == "tv" and (
                context.season is not None or context.episode is not None
            )
            detail = (
                self.get_detail(result.tmdb_id, result.media_type)
                if position_required else {}
            )
            mapping = (
                resolved_mapping
                if resolved_mapping is not None and result.tmdb_id == resolved_tmdb_id
                else infer_episode_mapping(
                    source_season=context.season,
                    source_episode=context.episode,
                    parent_path=context.parent_path,
                    detail=detail,
                    mode="auto",
                )
            )

            # 一些分割放送作品在发布组中写作 S02E01+，但 TMDB 把两段
            # 合并为唯一的 Season 01。普通位置校验会把它判成 season_not_found。
            # 这里只在候选达到全局严格阈值，或已经通过“唯一候选 + 强标题
            # + 候选间距 + 年份约束”的低分强证据预检时尝试换算。后者仍必须
            # 再由逐集播出日期的 >=42 天唯一停播间隔和最终 TMDB 集号校验共同
            # 证明；任何证据缺失都保持原映射并失败关闭，不降低普通匹配阈值。
            if (
                not mapping.changed
                and (
                    (
                        bool(decision.get("passed"))
                        and result.confidence >= 0.9
                    )
                    or proof_precheck
                )
                and result.media_type == "tv"
                and isinstance(context.season, int)
                and context.season >= 2
                and isinstance(context.episode, int)
                and context.episode >= 1
                and isinstance(detail, dict)
                and str(detail.get("id") or "").strip() == result.tmdb_id
            ):
                counts = season_episode_counts(detail)
                # 普通 TMDB 详情已经包含发布组声明的源季时无需请求逐集详情；
                # 合并 cour 映射只处理“源季不存在、但详情只有另一唯一季”的情况。
                if len(counts) == 1 and context.season not in counts:
                    target_season = next(iter(counts))
                    season_detail = self.get_tv_season_detail(
                        result.tmdb_id, target_season
                    )
                    cour_mapping = infer_merged_season_cour_mapping(
                        source_season=context.season,
                        source_episode=context.episode,
                        detail=detail,
                        season_detail=season_detail,
                    )
                    if cour_mapping.changed and cour_mapping.confidence >= 0.9:
                        cour_position = _validate_tmdb_position(
                            detail,
                            result.media_type,
                            cour_mapping.target_season,
                            cour_mapping.target_episode,
                        )
                        if (
                            cour_position.get("required")
                            and cour_position.get("passed")
                            and cour_position.get("reason") == "episode_verified"
                        ):
                            mapping = cour_mapping

            position = _validate_tmdb_position(
                detail, result.media_type,
                mapping.target_season, mapping.target_episode,
            )
            if mapping.changed and position.get("passed"):
                result.metadata = {
                    **dict(result.metadata or {}),
                    "episode_mapping": mapping.to_dict(),
                }
            proof = _build_verified_automatic_identity_proof(
                result=result,
                context=context,
                scored=scored,
                decision_constraints=decision_constraints,
                mapping=mapping,
                position=position,
                expected_year=expected_year,
            )
            if proof is not None:
                result.metadata = {
                    **dict(result.metadata or {}),
                    VERIFIED_AUTOMATIC_IDENTITY_PROOF_KEY: proof,
                }
            if position["required"] and not position["passed"]:
                reason = str(position.get("reason") or "position_unverified")
                result.status = "low_confidence"
                result.need_confirm = True
                result.error = _tmdb_position_error(position)
                constraint = f"tmdb_position_{reason}"
                if constraint not in result.rejected_constraints:
                    result.rejected_constraints.append(constraint)
                logger.info(
                    "  ⚠ TMDB 季集校验未通过 tmdb=%s season=%s episode=%s reason=%s",
                    result.tmdb_id,
                    position.get("season"),
                    position.get("episode"),
                    reason,
                )
                return result

        if decision["passed"]:
            result.status = "matched"
            result.need_confirm = False
            result.directory_identity_cache_eligible = bool(
                matched_by == "search"
                and str(result.provider or "").strip().lower() == "tmdb"
                and bool(result.tmdb_id)
            )
            if allow_lock:
                self._set_lock(
                    filename, result.tmdb_id, result.title or context.normalized_title,
                    result.year or expected_year, result.media_type,
                    parent_path=context.parent_path,
                )
            matched_query = (
                best.score_breakdown.matched_query if best.score_breakdown else ""
            )
            logger.info(
                f"  ✓ {matched_by} 匹配: {result.title} ({result.year}) "
                f"tmdb={result.tmdb_id} conf={result.confidence:.2f} "
                f"query={matched_query!r}"
            )
        else:
            result.status = "low_confidence"
            # 阈值未通过时必须进入人工确认，不能被旧的“预览确认”开关绕过。
            result.need_confirm = True
            if decision.get("reason") == "low_information_title":
                result.error = "标题信息量不足，候选结果需要人工确认"
            elif decision.get("reason") == "low_information_variant_match":
                result.error = "候选仅命中低信息目录或标题变体，无法证明与文件主标题一致，需要人工确认"
            elif decision.get("reason") == "animation_evidence_mismatch":
                result.error = "源文件包含 Donghua 动画标记，但 TMDB 候选不是动画条目，需要人工确认"
            elif decision.get("reason") == "ambiguous_romaji_alias":
                result.error = "同一罗马字/拉丁别名命中多个 TMDB 条目，需要人工确认"
            elif decision.get("reason") == "ambiguous_near_tie":
                result.error = "存在同名同年且得分接近的多个 TMDB 条目，需要人工确认"
            elif decision.get("reason") == "romaji_candidate_overflow":
                result.error = "拉丁标题候选过多，无法在安全上限内排除同名条目，需要人工确认"
            elif decision.get("reason") == "romaji_alias_coverage_incomplete":
                result.error = "部分 TMDB 候选详情不可用，无法排除同名条目，需要人工确认"
            elif decision.get("reason") == "distinctive_title_tokens_missing":
                result.error = "候选仅命中部分标题，完整标题仍有显著片段未匹配，需要人工确认"
            elif decision.get("reason") == "search_only_variant":
                result.error = "仅通过降噪补充查询找到候选，需人工确认媒体身份"
            else:
                result.error = (
                    f"匹配置信度 {result.confidence:.0%} 低于"
                    f"{'严格' if effective_match_mode == 'strict' else '宽松'}模式阈值 {threshold:.0%}"
                )
            matched_query = (
                best.score_breakdown.matched_query if best.score_breakdown else ""
            )
            logger.info(
                f"  ⚠ 确定性需确认 score={result.confidence:.2f} "
                f"reason={decision.get('reason')} title={result.title} "
                f"query={matched_query!r}"
            )
        return result

    def deterministic_recognize(
        self,
        filename: str,
        parent_path: str = "",
        *,
        media_type_hint: str = "",
        source_context: RecognitionContext | None = None,
    ) -> RecognitionResult:
        """执行不含 AI 的确定性 TMDB 搜索、评分与阈值决策。

        ``source_context`` 只保留管理员预处理前的目录来源；标题替换可以
        改善搜索召回，但不能重新让低信息根目录覆盖完整文件标题。
        """
        context = extract_recognition_context(filename, parent_path)
        _inherit_source_query_provenance(context, source_context)
        hint = str(media_type_hint or "").strip().lower()
        if hint in {"movie", "tv"}:
            context.media_type = hint
        primary = self._recognize_context(
            context, filename, allow_lock=False, matched_by="search"
        )
        if primary.status != "no_result" or context.media_type != "tv":
            return primary
        primary_attempts = list((primary.metadata or {}).get("search_attempts") or [])
        exact_title_key = _comparison_key(context.normalized_title)
        exact_title_fresh_empty = any(
            _comparison_key(str(item.get("query") or "")) == exact_title_key
            and str(item.get("year") or "") == ""
            and item.get("status") == "no_result"
            and not bool(item.get("cache_hit"))
            and not bool(item.get("empty_cache_hit"))
            for item in primary_attempts
            if isinstance(item, dict)
        )
        primary_query_failed = any(
            item.get("status") in {"config_error", "request_error"}
            for item in primary_attempts
            if isinstance(item, dict)
        )
        if not exact_title_fresh_empty or primary_query_failed:
            primary.metadata = {
                **dict(primary.metadata or {}),
                "implicit_season_fallback_skipped": "primary_title_unverified",
            }
            return primary

        fallback_context = _enclosed_high_season_fallback_context(context)
        if fallback_context is None:
            return primary
        fallback = self._recognize_context(
            fallback_context,
            filename,
            allow_lock=False,
            matched_by="implicit_season_fallback",
            match_mode="strict",
        )
        # 完整数字标题是第一解释；基础标题不仅要严格匹配和通过季集校验，
        # 还必须在多个同别名 TMDB 候选中由目标位置唯一消歧。单一基础标题
        # 即使恰好存在 S17E06，也不足以证明 ``Room 17`` 不是正式数字片名。
        position_resolution = dict(
            (fallback.metadata or {}).get("alias_position_resolution") or {}
        )
        if (
            fallback.status != "matched"
            or fallback.need_confirm
            or not fallback.tmdb_id
            or str(position_resolution.get("selected_tmdb_id") or "")
            != str(fallback.tmdb_id)
            or int(position_resolution.get("candidate_count") or 0) < 2
            or not list(position_resolution.get("excluded") or [])
        ):
            return primary
        fallback.season_override = fallback_context.season
        fallback.metadata = {
            **dict(fallback.metadata or {}),
            "implicit_season_fallback": {
                "source_title": context.normalized_title,
                "fallback_title": fallback_context.normalized_title,
                "season": fallback_context.season,
                "episode": fallback_context.episode,
                "primary_status": primary.status,
            },
        }
        return fallback

    def _external_hint_fallback(
        self,
        filename: str,
        parent_path: str,
        deterministic: RecognitionResult,
        *,
        source_anchors: list[str] | None = None,
    ) -> RecognitionResult:
        """在普通 TMDB 失败后，用外部标题线索重跑严格 TMDB 校验。"""
        if deterministic.status not in {"no_result", "low_confidence"}:
            return deterministic
        rejected = set(deterministic.rejected_constraints or [])
        if any(item.startswith("tmdb_position_") for item in rejected):
            # 季集越界属于原始 TMDB 硬冲突，不能用外部标题换一道搜索把
            # 问题“洗白”为另一个无关作品。近似同名则允许外部卡片提供标题
            # 线索，但仍必须经过源标题双向锚定与第二次 TMDB 严格复核。
            return deterministic
        context = deterministic.context or extract_recognition_context(filename, parent_path)
        search_anchors = [
            value for value in _unique_text((
                context.normalized_title, context.filename_title, context.folder_title,
                *context.title_variants,
            ))
            if value and not _low_information_query(value)
        ]
        if not search_anchors:
            return deterministic
        approval_anchors = list(source_anchors or _source_title_anchors(context))
        if not approval_anchors:
            return deterministic
        try:
            from app.modules.recognition_hints import search_recognition_hints

            hints = search_recognition_hints(search_anchors[0], context.media_type)
        except Exception as exc:
            logger.info("外部识别线索跳过 type=%s", type(exc).__name__)
            return deterministic
        expected_year = context.filename_year or context.folder_year
        for card in hints.items:
            if card.media_type not in {context.media_type, "all"}:
                continue
            if expected_year and card.year and str(card.year) != str(expected_year):
                continue
            titles = _unique_text((card.title, card.original_title))
            if not titles:
                continue
            (
                source_verified, source_score, matched_source,
                matched_hint_title, _,
            ) = _verify_source_title_anchor(
                approval_anchors,
                titles,
                season=context.season,
            )
            if not source_verified:
                continue
            hint_context = RecognitionContext(
                filename=context.filename,
                parent_path=context.parent_path,
                normalized_title=titles[0],
                filename_title=titles[0],
                filename_year=str(card.year or context.filename_year or ""),
                folder_title=titles[1] if len(titles) > 1 else titles[0],
                folder_year=str(card.year or context.folder_year or ""),
                media_type=context.media_type,
                season=context.season,
                episode=context.episode,
                title_variants=titles,
                cleaned_components=dict(context.cleaned_components),
            )
            second = self._recognize_context(
                hint_context,
                filename,
                allow_lock=False,
                matched_by=f"{card.provider}_hint",
                match_mode="strict",
            )
            # 外部源只提供搜索词；最终仍必须由 TMDB 严格评分通过。
            second_titles = _unique_text((
                second.title,
                *(candidate.title for candidate in (second.candidates or [])
                  if str(candidate.tmdb_id) == str(second.tmdb_id)),
                *(candidate.original_title for candidate in (second.candidates or [])
                  if str(candidate.tmdb_id) == str(second.tmdb_id)),
            ))
            # 外部卡片已经证明与源标题相关；二次 TMDB 结果还必须独立
            # 证明与该卡片相关，不能让已通过的卡片标题替无关 TMDB 结果背书。
            source_related = bool(second_titles) and max(
                (
                    _title_similarity_score(hint_title, result_title)
                    for hint_title in titles
                    for result_title in second_titles
                ),
                default=0.0,
            ) >= 0.72
            if (
                second.status == "matched"
                and not second.need_confirm
                and second.tmdb_id
                and second.media_type == context.media_type
                and second.confidence >= 0.9
                and source_related
            ):
                existing_metadata = dict(second.metadata or {})
                tmdb_evidence = dict(
                    existing_metadata.get("recognition_evidence") or {}
                )
                second.metadata = {
                    **existing_metadata,
                    "recognition_evidence": {
                        **tmdb_evidence,
                        "kind": "external_title_hint",
                        "provider": str(card.provider or ""),
                        "external_id": str(card.external_id or ""),
                        "hint_title": str(card.title or ""),
                        "hint_year": str(card.year or ""),
                        "source_anchor_verified": True,
                        "source": {
                            "filename_title": str(context.filename_title or ""),
                            "folder_title": str(context.folder_title or ""),
                            "approved_anchor": str(matched_source or ""),
                            "matched_hint_title": str(matched_hint_title or ""),
                            "anchor_score": float(round(source_score, 3)),
                        },
                        "tmdb_revalidated": True,
                        "tmdb_id": str(second.tmdb_id or ""),
                        "tmdb": {
                            "id": str(second.tmdb_id or ""),
                            "matched_query": str(
                                tmdb_evidence.get("matched_query") or ""
                            ),
                            "matched_title": str(
                                tmdb_evidence.get("matched_title") or ""
                            ),
                        },
                    },
                }
                logger.info(
                    "  ✓ 外部线索经 TMDB 复核通过 provider=%s tmdb=%s conf=%.2f",
                    card.provider, second.tmdb_id, second.confidence,
                )
                return second
        return deterministic

    @staticmethod
    def _ai_confidence_threshold() -> float:
        try:
            value = float(get("AI_RECOGNITION_CONFIDENCE_THRESHOLD", "0.8") or 0.8)
        except (TypeError, ValueError):
            value = 0.8
        return max(0.5, min(value, 0.99))

    @staticmethod
    def _ai_timeout_seconds() -> int:
        try:
            value = int(get("AGENT_LLM_TIMEOUT_SECONDS", "12") or 12)
        except (TypeError, ValueError):
            value = 12
        return max(2, min(value, 30))

    @staticmethod
    def _shared_ai_endpoint_is_valid(api_url: str) -> bool:
        """复用 Media Agent 连接时沿用其公共 HTTPS 安全边界。"""
        try:
            normalize_provider_location(api_url, https_only=True, public_only=True)
        except ValueError:
            return False
        return True

    @classmethod
    def _ai_provider_settings(cls) -> tuple[str, str, str, int, str]:
        """复用 Media Agent Provider，未配置时关闭 AI 回退。"""
        agent_url = str(get("AGENT_LLM_API_URL", "") or "").strip()
        agent_model = str(get("AGENT_LLM_MODEL", "") or "").strip()
        if agent_url and agent_model and cls._shared_ai_endpoint_is_valid(agent_url):
            return (
                agent_url,
                agent_model,
                str(get("AGENT_LLM_API_KEY", "") or ""),
                cls._ai_timeout_seconds(),
                resolve_protocol(get("AGENT_LLM_PROTOCOL", "auto"), agent_url),
            )
        return "", "", "", cls._ai_timeout_seconds(), "auto"

    @staticmethod
    def _ai_client_fingerprint(client_key: AIClientKey) -> str:
        protocol = client_key[5] if len(client_key) > 5 else "auto"
        try:
            base_url = normalize_provider_location(
                client_key[0], https_only=True, public_only=True
            ).base_url
        except ValueError:
            base_url = str(client_key[0] or "").strip()
        return provider_fingerprint(
            base_url=base_url,
            model=client_key[1],
            api_key=client_key[2],
            protocol=protocol or "auto",
        )

    @classmethod
    def _ai_cache_key(
        cls, payload: dict[str, object], client_key: AIClientKey, *, namespace: str
    ) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        identity = cls._ai_client_fingerprint(client_key)
        return hashlib.sha256(
            f"{namespace}\0{identity}\0{encoded}".encode("utf-8", "ignore")
        ).hexdigest()

    def _recognize_with_ai_cache(
        self,
        ai_input: AIRecognitionInput,
        *,
        client_key: AIClientKey,
        cache_key: str,
    ) -> AIRecognitionResult:
        """复用 AI 客户端并只合并相同输入；不同输入不互相阻塞。"""
        timeout_seconds = int(client_key[3])
        protocol = client_key[5] if len(client_key) > 5 else None
        scoped_client_key = (
            self._ai_client_fingerprint(client_key),
            timeout_seconds,
            threading.get_ident(),
        )
        while True:
            with self._ai_lock:
                current = time.monotonic()
                cached = self._ai_result_cache.get(cache_key)
                if cached and current - cached[0] <= _AI_RESULT_CACHE_TTL_SECONDS:
                    self._performance_counters["ai_cache_hits"] += 1
                    return cached[1]
                self._ai_result_cache.pop(cache_key, None)
                failed = self._active_ai_failure(
                    self._ai_failure_cache, cache_key, current
                )
                if failed:
                    raise _ai_failure_error_type(failed[2])(failed[1])

                in_flight = self._ai_inflight.get(cache_key)
                if in_flight is None:
                    in_flight = threading.Event()
                    self._ai_inflight[cache_key] = in_flight
                    owner = True
                    ai_client = self._ai_clients.get(scoped_client_key)
                    self._performance_counters["ai_requests"] += 1
                else:
                    owner = False
                    ai_client = None

            if not owner:
                if not in_flight.wait(timeout=max(5, timeout_seconds + 5)):
                    raise AIRecognitionError("AI 辅助识别等待超时")
                continue

            try:
                if ai_client is None:
                    new_client = AIRecognitionClient(
                        api_url=client_key[0],
                        api_key=client_key[2],
                        model=client_key[1],
                        protocol=protocol,
                        timeout_seconds=timeout_seconds,
                        proxy_url=client_key[4],
                    )
                    with self._ai_lock:
                        ai_client = self._ai_clients.get(scoped_client_key)
                        if ai_client is None:
                            if len(self._ai_clients) >= 8:
                                self._ai_clients.pop(next(iter(self._ai_clients)))
                            self._ai_clients[scoped_client_key] = new_client
                            ai_client = new_client
                result = ai_client.recognize(ai_input)
            except AIRecognitionError as exc:
                with self._ai_lock:
                    self._remember_ai_failure(
                        self._ai_failure_cache,
                        cache_key,
                        redact_sensitive_text(exc)[:500],
                        _ai_failure_kind(exc),
                    )
                    self._ai_inflight.pop(cache_key, None)
                    in_flight.set()
                raise
            except Exception as exc:
                safe_error = redact_sensitive_text(exc)[:500] or type(exc).__name__
                with self._ai_lock:
                    self._remember_ai_failure(
                        self._ai_failure_cache,
                        cache_key,
                        safe_error,
                        _AI_FAILURE_GENERIC,
                    )
                    self._ai_inflight.pop(cache_key, None)
                    in_flight.set()
                raise AIRecognitionError(f"AI 识别请求失败：{safe_error}") from None
            except BaseException:
                with self._ai_lock:
                    self._ai_inflight.pop(cache_key, None)
                    in_flight.set()
                raise

            with self._ai_lock:
                if len(self._ai_result_cache) >= 64:
                    self._ai_result_cache.pop(next(iter(self._ai_result_cache)))
                self._ai_result_cache[cache_key] = (time.monotonic(), result)
                self._ai_failure_cache.pop(cache_key, None)
                self._ai_inflight.pop(cache_key, None)
                in_flight.set()
            return result

    def _classify_release_group_with_ai_cache(
        self,
        ai_input: AIReleaseGroupInput,
        *,
        client_key: AIClientKey,
        cache_key: str,
    ) -> AIReleaseGroupResult:
        """对未知前置方括号做独立缓存分类，不污染媒体身份 AI 缓存。"""
        timeout_seconds = int(client_key[3])
        protocol = client_key[5] if len(client_key) > 5 else None
        scoped_client_key = (
            self._ai_client_fingerprint(client_key),
            timeout_seconds,
            threading.get_ident(),
        )
        while True:
            with self._ai_lock:
                current = time.monotonic()
                cached = self._release_group_ai_cache.get(cache_key)
                if cached and current - cached[0] <= _AI_RESULT_CACHE_TTL_SECONDS:
                    self._performance_counters["ai_cache_hits"] += 1
                    return cached[1]
                self._release_group_ai_cache.pop(cache_key, None)
                failed = self._active_ai_failure(
                    self._release_group_ai_failure_cache, cache_key, current
                )
                if failed:
                    raise _ai_failure_error_type(failed[2])(failed[1])

                in_flight = self._release_group_ai_inflight.get(cache_key)
                if in_flight is None:
                    in_flight = threading.Event()
                    self._release_group_ai_inflight[cache_key] = in_flight
                    owner = True
                    ai_client = self._ai_clients.get(scoped_client_key)
                    self._performance_counters["ai_requests"] += 1
                else:
                    owner = False
                    ai_client = None

            if not owner:
                if not in_flight.wait(timeout=max(5, timeout_seconds + 5)):
                    raise AIRecognitionError("AI 发布组分类等待超时")
                continue

            try:
                if ai_client is None:
                    new_client = AIRecognitionClient(
                        api_url=client_key[0],
                        api_key=client_key[2],
                        model=client_key[1],
                        protocol=protocol,
                        timeout_seconds=timeout_seconds,
                        proxy_url=client_key[4],
                    )
                    with self._ai_lock:
                        ai_client = self._ai_clients.get(scoped_client_key)
                        if ai_client is None:
                            if len(self._ai_clients) >= 8:
                                self._ai_clients.pop(next(iter(self._ai_clients)))
                            self._ai_clients[scoped_client_key] = new_client
                            ai_client = new_client
                result = ai_client.classify_release_group(ai_input)
            except AIRecognitionError as exc:
                safe_error = redact_sensitive_text(exc)[:500]
                with self._ai_lock:
                    self._remember_ai_failure(
                        self._release_group_ai_failure_cache,
                        cache_key,
                        safe_error,
                        _ai_failure_kind(exc),
                    )
                    self._release_group_ai_inflight.pop(cache_key, None)
                    in_flight.set()
                raise
            except Exception as exc:
                safe_error = redact_sensitive_text(exc)[:500] or type(exc).__name__
                with self._ai_lock:
                    self._remember_ai_failure(
                        self._release_group_ai_failure_cache,
                        cache_key,
                        safe_error,
                        _AI_FAILURE_GENERIC,
                    )
                    self._release_group_ai_inflight.pop(cache_key, None)
                    in_flight.set()
                raise AIRecognitionError(f"AI 发布组分类失败：{safe_error}") from None
            except BaseException:
                with self._ai_lock:
                    self._release_group_ai_inflight.pop(cache_key, None)
                    in_flight.set()
                raise

            with self._ai_lock:
                if len(self._release_group_ai_cache) >= 64:
                    self._release_group_ai_cache.pop(next(iter(self._release_group_ai_cache)))
                self._release_group_ai_cache[cache_key] = (time.monotonic(), result)
                self._release_group_ai_failure_cache.pop(cache_key, None)
                self._release_group_ai_inflight.pop(cache_key, None)
                in_flight.set()
            return result

    @staticmethod
    def _unknown_release_group_candidate(filename: str) -> tuple[str, str, str] | None:
        match = _RELEASE_PREFIX.match(str(filename or ""))
        if not match:
            return None
        raw_token = match.group(1)
        content = _bracket_content(raw_token)
        remainder = str(filename or "")[match.end():].lstrip()
        if not content or not remainder or _is_release_prefix(content, remainder):
            return None
        # 停用词条代表用户明确否决，不能被 AI 作为“未知发布组”重新学习并绕过。
        try:
            from app.modules.recognition_knowledge import lookup_any

            if lookup_any(content, "release_group"):
                return None
        except Exception as exc:
            logger.warning("本地识别词库完整查询失败 type=%s", type(exc).__name__)
            return None
        compact = re.sub(r"\s+", " ", content).strip()
        if not 3 <= len(compact) <= 80:
            return None
        if re.fullmatch(r"[\d ._-]+", compact):
            return None
        if compact.casefold() in {"a", "an", "the", "of", "movie", "tv", "anime"}:
            return None
        if _RELEASE_SOURCE_BRACKET.fullmatch(compact) or _is_bracket_noise(compact):
            return None
        if not _probable_unknown_release_prefix(compact, remainder):
            return None
        return compact, remainder, raw_token

    def _release_group_fallback(
        self,
        filename: str,
        parent_path: str,
        deterministic: RecognitionResult,
        *,
        media_type_hint: str = "",
    ) -> RecognitionResult:
        """未知发布组仅在 AI 高置信 + 去前缀后 TMDB 严格命中时即时采用。"""
        if not get_bool("AI_RECOGNITION_ENABLED", False):
            return deterministic
        if deterministic.status not in {"no_result", "low_confidence"}:
            return deterministic
        candidate = self._unknown_release_group_candidate(filename)
        if candidate is None:
            return deterministic
        group_token, remainder, raw_token = candidate
        api_url, model, api_key, timeout_seconds, protocol = self._ai_provider_settings()
        if not api_url or not model:
            return deterministic
        ai_input = AIReleaseGroupInput(
            group_token=group_token, remainder_title=remainder
        )
        try:
            proxy_url = str(get("PROXY_URL", "") or "")
            client_key = (api_url, model, api_key, timeout_seconds, proxy_url, protocol)
            cache_key = self._ai_cache_key(
                ai_input.safe_payload(), client_key, namespace="release_group"
            )
            ai_result = self._classify_release_group_with_ai_cache(
                ai_input, client_key=client_key, cache_key=cache_key
            )
        except AIRecognitionError as exc:
            logger.info(
                "未知发布组 AI 分类未完成 group=%s reason=%s",
                redact_sensitive_text(group_token)[:80], redact_sensitive_text(exc)[:160],
            )
            return deterministic

        threshold = max(0.95, self._ai_confidence_threshold())
        if not ai_result.is_release_group or ai_result.confidence < threshold:
            return deterministic
        hint = media_type_hint if media_type_hint in {"movie", "tv"} else ""
        retry = (
            self.deterministic_recognize(remainder, "", media_type_hint=hint)
            if hint else self.deterministic_recognize(remainder, "")
        )
        if not (
            retry.status == "matched"
            and not retry.need_confirm
            and retry.tmdb_id
            and retry.confidence >= 0.9
        ):
            return deterministic
        if retry.context is not None:
            cleaned = dict(retry.context.cleaned_components)
            cleaned["release_prefixes"] = _unique_text(
                [raw_token, *cleaned.get("release_prefixes", [])]
            )
            retry.context.cleaned_components = cleaned
        retry.matched_by = "ai_release_group"
        learning = {
            "group_token": group_token,
            "canonical_name": ai_result.canonical_name or group_token,
            "confidence": ai_result.confidence,
            "verified_tmdb_id": retry.tmdb_id,
            "enabled_after_samples": 2,
        }
        try:
            from app.modules.recognition_knowledge import record_learned_release_group

            sample_key = hashlib.sha256(
                f"{group_token}\0{retry.tmdb_id}\0{retry.context.normalized_title if retry.context else remainder}".encode(
                    "utf-8", "ignore"
                )
            ).hexdigest()[:24]
            learned = record_learned_release_group(
                group_token,
                confidence=ai_result.confidence,
                aliases=list(ai_result.aliases),
                evidence={
                    "sample_key": sample_key,
                    "aliases": list(ai_result.aliases),
                    "tmdb_id": retry.tmdb_id,
                    "title": retry.title,
                    "year": retry.year,
                },
            )
            learning.update(
                learned_id=learned.get("id"),
                active=not bool(learned.get("disabled", True)),
                sample_count=int(learned.get("success_count") or 0),
            )
        except Exception as exc:
            learning["record_error"] = type(exc).__name__
            logger.warning("识别知识学习记录失败 type=%s", type(exc).__name__)
        retry.metadata = {**dict(retry.metadata), "knowledge_learning": learning}
        return retry

    def _record_tavily_hint_result(self, result: object) -> None:
        """记录网页标题线索的逻辑查找、真实请求与缓存命中。"""
        with self._tmdb_state_lock:
            self._performance_counters["tavily_hint_lookups"] += 1
            if bool(getattr(result, "cached", False)):
                self._performance_counters["tavily_hint_cache_hits"] += 1
            elif bool(getattr(result, "attempted", False)):
                self._performance_counters["tavily_hint_requests"] += 1

    def _record_tavily_hint_match(self) -> None:
        with self._tmdb_state_lock:
            self._performance_counters["tavily_hint_matches"] += 1

    def _search_tavily_title_hints(
        self,
        query: str,
        *,
        media_type: str,
        year: str,
        diagnostic: dict[str, object],
        log_context: str,
    ):
        """请求网页标题线索并统一记录可用性、缓存与性能诊断。"""
        try:
            from app.modules.recognition_web_hints import search_recognition_titles

            hints = search_recognition_titles(
                query, media_type=media_type, year=year
            )
        except Exception as exc:
            logger.info("%s跳过 type=%s", log_context, type(exc).__name__)
            diagnostic.update(
                attempted=True,
                status="unavailable",
                error="网页标题线索暂时不可用",
            )
            return None

        self._record_tavily_hint_result(hints)
        diagnostic.update(
            attempted=bool(hints.attempted),
            status=str(hints.status or "unavailable"),
            cached=bool(hints.cached),
            title_count=len(hints.titles),
            error=str(hints.error or "")[:240],
        )
        return hints

    def _tavily_title_hint_fallback(
        self,
        filename: str,
        parent_path: str,
        deterministic: RecognitionResult,
        *,
        diagnostic: dict[str, object] | None = None,
        source_anchors: list[str] | None = None,
    ) -> RecognitionResult:
        """AI Provider 真正不可用时，用 Tavily 标题线索重走严格 TMDB 复核。

        网页结果只提供标题字符串，不能提供 TMDB ID、媒体类型、年份或归档
        位置。任何来源锚点、TMDB 详情、年份、季集位置证据不足都会保留原始
        待确认结果；这条回退不能修饰已有 TMDB 歧义或越界冲突。
        """
        safe_diagnostic = dict(diagnostic or {})
        web_diagnostic: dict[str, object] = {
            "attempted": False,
            "status": "not_eligible",
            "cached": False,
            "title_count": 0,
            "error": "",
        }
        safe_diagnostic["web_title_hint"] = web_diagnostic
        deterministic.ai_diagnostic = safe_diagnostic
        if deterministic.status not in {"no_result", "low_confidence"}:
            return deterministic
        rejected = set(deterministic.rejected_constraints or [])
        if "ambiguous_near_tie" in rejected or any(
            item.startswith("tmdb_position_") for item in rejected
        ):
            web_diagnostic["status"] = "deterministic_conflict"
            return deterministic

        context = deterministic.context or extract_recognition_context(
            filename, parent_path
        )
        search_anchors = [
            value
            for value in _unique_text(
                (
                    context.normalized_title,
                    context.filename_title,
                    context.folder_title,
                    *context.title_variants,
                )
            )
            if value and not _low_information_query(value)
        ]
        if not search_anchors:
            web_diagnostic["status"] = "no_source_anchor"
            return deterministic
        approval_anchors = list(source_anchors or _source_title_anchors(context))
        if not approval_anchors:
            web_diagnostic["status"] = "no_original_source_anchor"
            return deterministic

        expected_year = str(context.filename_year or context.folder_year or "")
        hints = self._search_tavily_title_hints(
            search_anchors[0],
            media_type=context.media_type,
            year=expected_year,
            diagnostic=web_diagnostic,
            log_context="整理网页标题线索",
        )
        if hints is None or not hints.titles:
            return deterministic

        for hint_title in hints.titles:
            source_hint_score = max(
                (
                    _title_similarity_score(anchor, hint_title)
                    for anchor in search_anchors
                    if anchor and hint_title
                ),
                default=0.0,
            )
            if source_hint_score < 0.66:
                continue
            hint_context = RecognitionContext(
                filename=context.filename,
                parent_path=context.parent_path,
                normalized_title=hint_title,
                filename_title=hint_title,
                filename_year=expected_year,
                folder_title=context.folder_title or hint_title,
                folder_year=expected_year,
                media_type=context.media_type,
                season=context.season,
                episode=context.episode,
                title_variants=_unique_text((hint_title, *search_anchors)),
                cleaned_components=dict(context.cleaned_components),
            )
            second = self._recognize_context(
                hint_context,
                filename,
                allow_lock=False,
                matched_by="tavily_hint_search",
                match_mode="strict",
            )
            if not (
                second.status == "matched"
                and not second.need_confirm
                and second.tmdb_id
                and second.media_type == context.media_type
                and second.confidence >= 0.9
            ):
                continue

            detail = self.get_detail(second.tmdb_id, second.media_type)
            detail_title = str(
                detail.get("name") or detail.get("title") or ""
            ).strip()
            detail_original = str(
                detail.get("original_name") or detail.get("original_title") or ""
            ).strip()
            detail_date = str(
                detail.get("first_air_date") or detail.get("release_date") or ""
            )
            detail_year = detail_date[:4]
            selected_candidate = next(
                (
                    candidate
                    for candidate in second.candidates
                    if str(candidate.tmdb_id) == str(second.tmdb_id)
                ),
                None,
            )
            candidate_titles = _unique_text(
                (
                    detail_title,
                    detail_original,
                    *(selected_candidate.aliases if selected_candidate else ()),
                )
            )
            hint_verified = max(
                (
                    _title_similarity_score(hint_title, candidate_title)
                    for candidate_title in candidate_titles
                    if candidate_title
                ),
                default=0.0,
            ) >= 0.9
            (
                source_verified,
                source_score,
                matched_source,
                matched_candidate,
                source_anchor_reason,
            ) = _verify_source_title_anchor(
                approval_anchors,
                candidate_titles,
                season=context.season,
            )
            year_verified = bool(
                not expected_year
                or (detail_year and detail_year == expected_year)
            )
            id_verified = str(detail.get("id") or "") == str(second.tmdb_id)
            mapping = infer_episode_mapping(
                source_season=context.season,
                source_episode=context.episode,
                parent_path=context.parent_path,
                detail=detail,
                mode="auto",
            )
            position = _validate_tmdb_position(
                detail,
                second.media_type,
                mapping.target_season,
                mapping.target_episode,
            )
            passed = bool(
                detail
                and hint_verified
                and source_verified
                and year_verified
                and id_verified
                and position.get("passed")
            )
            web_diagnostic["tmdb_revalidation"] = {
                "passed": passed,
                "source_hint_score": round(source_hint_score, 3),
                "hint_verified": hint_verified,
                "source_anchor": {
                    "passed": source_verified,
                    "score": round(source_score, 3),
                    "matched_source": matched_source,
                    "matched_candidate": matched_candidate,
                    "reason": source_anchor_reason,
                    "input_origin": "raw" if source_anchors is not None else "processed_fallback",
                    "anchor_count": len(approval_anchors),
                },
                "year_verified": year_verified,
                "id_verified": id_verified,
                "position": position,
            }
            if not passed:
                continue
            if mapping.changed:
                second.metadata = {
                    **dict(second.metadata or {}),
                    "episode_mapping": mapping.to_dict(),
                }
            second.status = "matched"
            second.matched_by = "tavily_tmdb_revalidated"
            second.need_confirm = False
            second.error = ""
            second.title = detail_title or second.title
            second.year = detail_year or second.year
            second.ai_diagnostic = safe_diagnostic
            self._record_tavily_hint_match()
            logger.info(
                "  ✓ Tavily 标题线索经 TMDB 严格复核通过 tmdb=%s conf=%.2f",
                second.tmdb_id,
                second.confidence,
            )
            return second
        return deterministic

    def _tavily_ai_tmdb_corroboration(
        self,
        filename: str,
        deterministic: RecognitionResult,
        second: RecognitionResult,
        *,
        source_context: RecognitionContext,
        ai_context: RecognitionContext,
        ai_result: AIRecognitionResult,
        candidate_titles: list[str],
        source_anchors: list[str],
        detail_year: str,
        position: dict[str, object],
        diagnostic: dict[str, object],
    ) -> bool:
        """用独立网页标题桥接来源标题与 AI/TMDB 同一实体。

        Tavily 只能提供标题字符串。自动通过仍要求：来源标题与网页标题、网页
        标题与 TMDB 标题、AI 标题与 TMDB 标题分别达到固定下限；网页标题
        还必须重新经过严格 TMDB 搜索并返回同一 ID。季集位置、年份和类型继续
        使用调用方已经验证的确定性证据。
        """
        tmdb_revalidation = diagnostic.get("tmdb_revalidation")
        if not isinstance(tmdb_revalidation, dict):
            return False
        corroboration: dict[str, object] = {
            "attempted": False,
            "status": "not_eligible",
            "cached": False,
            "title_count": 0,
            "passed": False,
            "error": "",
        }
        tmdb_revalidation["tavily_corroboration"] = corroboration

        rejected = set(deterministic.rejected_constraints or []) | set(
            second.rejected_constraints or []
        )
        if "ambiguous_near_tie" in rejected or any(
            item.startswith("tmdb_position_") for item in rejected
        ):
            corroboration["status"] = "deterministic_conflict"
            return False
        if not source_anchors:
            corroboration["status"] = "no_source_anchor"
            return False
        if not position.get("passed"):
            corroboration["status"] = "position_conflict"
            return False

        ai_titles = [
            value
            for value in _unique_text((
                ai_result.title, ai_result.original_title, *ai_result.aliases,
                *ai_context.title_variants,
            ))
            if value
        ]
        ai_candidate_score = max(
            (
                _title_similarity_score(ai_title, candidate_title)
                for ai_title in ai_titles
                for candidate_title in candidate_titles
                if ai_title and candidate_title
            ),
            default=0.0,
        )
        corroboration["ai_tmdb_score"] = round(ai_candidate_score, 3)
        if ai_candidate_score < 0.9:
            corroboration["status"] = "ai_tmdb_title_mismatch"
            return False

        hints = self._search_tavily_title_hints(
            ai_result.title,
            media_type=second.media_type,
            year=str(ai_result.year or detail_year or ""),
            diagnostic=corroboration,
            log_context="整理三方标题复核",
        )
        if hints is None or not hints.titles:
            return False

        expected_year = str(ai_result.year or detail_year or "")
        best_scores = (0.0, 0.0)
        for hint_title in hints.titles:
            source_hint_score = max(
                (
                    _title_similarity_score(source, hint_title)
                    for source in source_anchors
                    if source and hint_title
                ),
                default=0.0,
            )
            hint_candidate_score = max(
                (
                    _title_similarity_score(hint_title, candidate_title)
                    for candidate_title in candidate_titles
                    if hint_title and candidate_title
                ),
                default=0.0,
            )
            best_scores = max(
                best_scores,
                (source_hint_score, hint_candidate_score),
                key=lambda item: (min(item), sum(item)),
            )
            # 双语网页标题常把罗马字/中文名与英文名并列；0.82 可以容纳
            # 这种受限拼接，但不足以让普通片段或单个关键词成为自动证据。
            if source_hint_score < 0.82 or hint_candidate_score < 0.82:
                continue
            hint_context = RecognitionContext(
                filename=source_context.filename,
                parent_path=source_context.parent_path,
                normalized_title=hint_title,
                filename_title=hint_title,
                filename_year=expected_year,
                folder_title=hint_title,
                folder_year=expected_year,
                media_type=source_context.media_type,
                season=source_context.season,
                episode=source_context.episode,
                title_variants=[hint_title],
                cleaned_components=dict(source_context.cleaned_components),
            )
            strict = self._recognize_context(
                hint_context,
                filename,
                allow_lock=False,
                matched_by="ai_tavily_hint_search",
                match_mode="strict",
            )
            strict_same_id = bool(
                strict.status == "matched"
                and not strict.need_confirm
                and strict.tmdb_id
                and str(strict.tmdb_id) == str(second.tmdb_id)
                and strict.media_type == second.media_type
                and strict.confidence >= 0.9
            )
            corroboration.update(
                source_web_score=round(source_hint_score, 3),
                web_tmdb_score=round(hint_candidate_score, 3),
                matched_hint=str(hint_title)[:200],
                strict_tmdb_id=str(strict.tmdb_id or ""),
                strict_confidence=round(float(strict.confidence or 0.0), 3),
                strict_same_id=strict_same_id,
            )
            if not strict_same_id:
                corroboration["status"] = "strict_tmdb_mismatch"
                continue

            corroboration.update(passed=True, status="matched")
            second.metadata = {
                **dict(second.metadata or {}),
                "recognition_evidence": {
                    "mode": "llm_tavily_tmdb",
                    "tmdb_id": str(second.tmdb_id),
                    "llm_confidence": round(float(ai_result.confidence), 3),
                    "source_web_score": round(source_hint_score, 3),
                    "web_tmdb_score": round(hint_candidate_score, 3),
                    "strict_tmdb_confidence": round(float(strict.confidence), 3),
                },
            }
            self._record_tavily_hint_match()
            return True

        corroboration.setdefault("source_web_score", round(best_scores[0], 3))
        corroboration.setdefault("web_tmdb_score", round(best_scores[1], 3))
        if corroboration.get("status") not in {"strict_tmdb_mismatch"}:
            corroboration["status"] = "insufficient_bridge_evidence"
        return False

    def _ai_fallback(
        self,
        filename: str,
        parent_path: str,
        deterministic: RecognitionResult,
        *,
        source_anchors: list[str] | None = None,
    ) -> RecognitionResult:
        if not get_bool("AI_RECOGNITION_ENABLED", False):
            deterministic.ai_diagnostic = {
                "attempted": False,
                "reason": "disabled",
            }
            return deterministic
        if deterministic.status not in {"no_result", "low_confidence"}:
            deterministic.ai_diagnostic = {
                "attempted": False,
                "reason": "deterministic_not_eligible",
            }
            return deterministic

        context = deterministic.context or extract_recognition_context(filename, parent_path)
        ai_input = AIRecognitionInput(
            normalized_title=context.normalized_title,
            filename_title=context.filename_title,
            folder_title=context.folder_title,
            folder_year=context.folder_year,
            media_type=context.media_type,
            season=context.season,
            episode=context.episode,
            aliases=tuple(context.title_variants),
        )
        threshold = self._ai_confidence_threshold()
        try:
            safe_input = ai_input.safe_payload()
        except AIRecognitionError as exc:
            deterministic.ai_diagnostic = {
                "attempted": False,
                "reason": "unsafe_input",
                "input": {},
                "output": {},
                "confidence_threshold": threshold,
                "error": redact_sensitive_text(exc)[:500],
            }
            return deterministic
        diagnostic: dict[str, object] = {
            "attempted": True,
            "reason": "deterministic_failed",
            "input": safe_input,
            "output": {},
            "confidence_threshold": threshold,
            "error": "",
        }
        api_url, model, api_key, timeout_seconds, protocol = self._ai_provider_settings()
        if not api_url or not model:
            diagnostic.update(
                attempted=False,
                reason="misconfigured",
                error="请先在 Media Agent 设置中配置模型连接",
            )
            deterministic.ai_diagnostic = diagnostic
            return deterministic
        try:
            proxy_url = str(get("PROXY_URL", "") or "")
            client_key = (api_url, model, api_key, timeout_seconds, proxy_url, protocol)
            cache_key = self._ai_cache_key(
                safe_input, client_key, namespace="media_identity"
            )
            ai_result = self._recognize_with_ai_cache(
                ai_input, client_key=client_key, cache_key=cache_key,
            )
        except AIRecognitionUnavailableError as exc:
            diagnostic["error"] = redact_sensitive_text(exc)[:500]
            deterministic.ai_diagnostic = diagnostic
            return self._tavily_title_hint_fallback(
                filename,
                parent_path,
                deterministic,
                diagnostic=diagnostic,
                source_anchors=source_anchors,
            )
        except AIRecognitionProviderError as exc:
            diagnostic["error"] = redact_sensitive_text(exc)[:500]
            deterministic.ai_diagnostic = diagnostic
            return deterministic
        except AIRecognitionError as exc:
            diagnostic["error"] = redact_sensitive_text(exc)[:500]
            deterministic.ai_diagnostic = diagnostic
            return deterministic

        diagnostic["output"] = ai_result.safe_payload()
        ai_context = RecognitionContext(
            filename=context.filename,
            parent_path=context.parent_path,
            normalized_title=ai_result.title,
            filename_title=ai_result.title,
            filename_year=str(ai_result.year or ""),
            folder_title=context.folder_title,
            folder_year=str(ai_result.year or context.folder_year or ""),
            # AI 只纠正片名/年份查询，不得覆盖确定性季集与媒体类型。
            media_type=context.media_type,
            season=context.season,
            episode=context.episode,
            title_variants=_unique_text((
                ai_result.title,
                ai_result.original_title,
                *ai_result.aliases,
            )),
            cleaned_components=dict(context.cleaned_components),
        )
        second = self._recognize_context(
            ai_context,
            filename,
            allow_lock=False,
            matched_by="ai_search",
        )
        diagnostic["second_search"] = {
            "candidate_count": len(second.candidates),
            "threshold_decision": dict(second.threshold_decision),
        }
        if (
            ai_result.media_type != context.media_type
            or (ai_result.season is not None and ai_result.season != context.season)
            or (ai_result.episode is not None and ai_result.episode != context.episode)
        ):
            diagnostic["position_guard"] = {
                "ignored_media_type": ai_result.media_type,
                "kept_media_type": context.media_type,
                "ignored_season": ai_result.season,
                "ignored_episode": ai_result.episode,
                "kept_season": context.season,
                "kept_episode": context.episode,
            }
        second.ai_diagnostic = diagnostic
        local_passed = bool(second.threshold_decision.get("passed"))
        # AI 自动通过采用固定严格下限；宽松匹配模式不能降低这条安全线。
        local_strict_passed = second.confidence >= 0.9
        ai_passed = ai_result.confidence >= threshold
        # 82%–89% 的候选只能进入 LLM + Tavily + TMDB 三方复核，不能仅凭
        # AI 或一次宽松 TMDB 搜索自动整理。最终仍必须由网页标题桥接和
        # strict TMDB 同 ID 复核通过；更低分候选继续人工确认。
        corroboration_candidate = bool(
            ai_passed
            and second.tmdb_id
            and second.media_type in {"movie", "tv"}
            and second.confidence >= 0.82
        )
        detail_revalidation_eligible = bool(
            (local_passed and local_strict_passed) or corroboration_candidate
        )
        source_year_verified = True
        detail_available = False
        position_validation: dict[str, object] | None = None
        if detail_revalidation_eligible and ai_passed and second.tmdb_id:
            detail = self.get_detail(second.tmdb_id, second.media_type)
            detail_available = bool(detail)
            detail_title = str(detail.get("name") or detail.get("title") or "").strip()
            detail_original = str(
                detail.get("original_name") or detail.get("original_title") or ""
            ).strip()
            detail_date = str(
                detail.get("first_air_date") or detail.get("release_date") or ""
            )
            detail_year = detail_date[:4]
            matching_candidate_aliases = [
                alias
                for candidate in second.candidates
                if str(candidate.tmdb_id or "") == str(second.tmdb_id or "")
                for alias in candidate.aliases
            ]
            # 详情主标题不足以覆盖跨语言发布名。只扩充 TMDB 详情返回的
            # alternative_titles/translations，以及同一 TMDB ID 搜索候选中的
            # 官方别名；阈值仍保持 90%，不信任 LLM 自报别名直接放行。
            candidate_titles = _unique_text((
                detail_title,
                detail_original,
                *_candidate_aliases(detail),
                *matching_candidate_aliases,
            ))
            title_verified = max(
                (_title_similarity_score(query, title)
                 for query in ai_context.title_variants
                 for title in candidate_titles if query and title),
                default=0.0,
            ) >= 0.9
            # 来源锚点必须来自原始解析字段。title_variants 仍可参与召回和
            # TMDB 标题复核，但不能作为“原始标题已被完整解释”的硬证据；
            # 否则从包装标题拆出的短变体会绕过显著残片保护。
            approval_anchors = list(
                source_anchors or _source_title_anchors(context)
            )
            (
                source_anchor_verified,
                source_score,
                matched_source,
                matched_candidate,
                source_anchor_reason,
            ) = _verify_source_title_anchor(
                approval_anchors,
                candidate_titles,
                season=context.season,
            )
            year_verified = (
                ai_result.year is None
                or (bool(detail_year) and detail_year == str(ai_result.year))
            )
            source_year = str(context.filename_year or context.folder_year or "")
            id_verified = str(detail.get("id") or "") == str(second.tmdb_id)
            episode_mapping = infer_episode_mapping(
                source_season=ai_context.season,
                source_episode=ai_context.episode,
                parent_path=ai_context.parent_path,
                detail=detail,
                mode="auto",
            )

            # AI 只负责修正标题查询，季集仍以确定性解析为准。若发布方把
            # 分割放送写成 S02/S03，而 TMDB 合并为唯一常规季，则复用与
            # deterministic 路径相同的逐集播出日期证明；没有完整 air_date、
            # 唯一停播边界或最终集号校验时保持原位置并失败关闭。
            if (
                not episode_mapping.changed
                and second.media_type == "tv"
                and isinstance(ai_context.season, int)
                and ai_context.season >= 2
                and isinstance(ai_context.episode, int)
                and ai_context.episode >= 1
                and id_verified
            ):
                counts = season_episode_counts(detail)
                if len(counts) == 1 and ai_context.season not in counts:
                    target_season = next(iter(counts))
                    season_detail = self.get_tv_season_detail(
                        second.tmdb_id, target_season
                    )
                    cour_mapping = infer_merged_season_cour_mapping(
                        source_season=ai_context.season,
                        source_episode=ai_context.episode,
                        detail=detail,
                        season_detail=season_detail,
                    )
                    if cour_mapping.changed and cour_mapping.confidence >= 0.9:
                        cour_position = _validate_tmdb_position(
                            detail,
                            second.media_type,
                            cour_mapping.target_season,
                            cour_mapping.target_episode,
                        )
                        if (
                            cour_position.get("required")
                            and cour_position.get("passed")
                            and cour_position.get("reason") == "episode_verified"
                        ):
                            episode_mapping = cour_mapping

            position = _validate_tmdb_position(
                detail, second.media_type,
                episode_mapping.target_season, episode_mapping.target_episode,
            )
            position_validation = position
            source_year_verified, source_year_mode = _source_year_matches_tmdb(
                detail,
                second.media_type,
                source_year,
                target_season=episode_mapping.target_season,
            )
            if (
                not source_year_verified
                and source_year_mode == "target_season_year_mismatch"
                and second.media_type == "tv"
                and isinstance(episode_mapping.target_season, int)
                and episode_mapping.target_season >= 0
                and isinstance(episode_mapping.target_episode, int)
                and episode_mapping.target_episode >= 1
            ):
                # AI 慢路径必须与确定性候选消歧使用相同的跨年季证据。
                # 仅在季年份冲突时补取逐集详情，避免为常规识别增加请求。
                source_year_verified, source_year_mode = _source_year_matches_tmdb(
                    detail,
                    second.media_type,
                    source_year,
                    target_season=episode_mapping.target_season,
                    target_episode=episode_mapping.target_episode,
                    season_episodes=self._season_episodes(
                        second.tmdb_id, episode_mapping.target_season,
                    ),
                )
            if episode_mapping.changed and position.get("passed"):
                second.metadata = {
                    **dict(second.metadata or {}),
                    "episode_mapping": episode_mapping.to_dict(),
                }
            revalidation_passed = bool(
                detail
                and title_verified
                and source_anchor_verified
                and year_verified
                and source_year_verified
                and id_verified
                and position["passed"]
            )
            diagnostic["tmdb_revalidation"] = {
                "passed": revalidation_passed,
                "title_verified": title_verified,
                "source_anchor": {
                    "passed": source_anchor_verified,
                    "score": round(source_score, 3),
                    "matched_source": matched_source,
                    "matched_candidate": matched_candidate,
                    "reason": source_anchor_reason,
                    "input_origin": "raw" if source_anchors is not None else "processed_fallback",
                    "anchor_count": len(approval_anchors),
                },
                "year_verified": year_verified,
                "source_year_verified": source_year_verified,
                "source_year": source_year,
                "source_year_mode": source_year_mode,
                "id_verified": id_verified,
                "detail_year": detail_year,
                "position": position,
            }
            if revalidation_passed and local_passed and local_strict_passed:
                second.status = "matched"
                second.matched_by = "ai_tmdb_revalidated"
                second.need_confirm = False
                second.error = ""
                second.title = detail_title or second.title
                second.year = detail_year or second.year
                return second

            # 以下两类标题型困难样本可以请求 Tavily 作为独立“别名桥”：
            # 1) AI/TMDB 已严格通过，唯独原始标题不能直接锚定候选；
            # 2) AI 高置信且 TMDB 候选达到 82%，但未达到 90% 自动线。
            # 两类都仍需网页标题双向桥接，并由 strict TMDB 重搜返回同一 ID；
            # Tavily 不能覆盖年份、季集位置、类型或候选歧义等硬冲突。
            needs_title_corroboration = bool(
                not source_anchor_verified or not local_strict_passed or not local_passed
            )
            if needs_title_corroboration and not position["passed"]:
                tmdb_revalidation = diagnostic.get("tmdb_revalidation")
                if isinstance(tmdb_revalidation, dict):
                    tmdb_revalidation["tavily_corroboration"] = {
                        "attempted": False,
                        "passed": False,
                        "status": "position_conflict",
                        "reason": str(position.get("reason") or "position_unverified"),
                    }
            corroboration_passed = bool(
                detail
                and title_verified
                and needs_title_corroboration
                and approval_anchors
                and year_verified
                and source_year_verified
                and id_verified
                and position["passed"]
                and self._tavily_ai_tmdb_corroboration(
                    filename,
                    deterministic,
                    second,
                    source_context=context,
                    ai_context=ai_context,
                    ai_result=ai_result,
                    candidate_titles=candidate_titles,
                    source_anchors=approval_anchors,
                    detail_year=detail_year,
                    position=position,
                    diagnostic=diagnostic,
                )
            )
            if corroboration_passed:
                tmdb_revalidation = diagnostic.get("tmdb_revalidation")
                if isinstance(tmdb_revalidation, dict):
                    tmdb_revalidation["passed"] = True
                    tmdb_revalidation["resolution_mode"] = "llm_tavily_tmdb"
                second.status = "matched"
                second.matched_by = "ai_tavily_tmdb_revalidated"
                second.need_confirm = False
                second.error = ""
                second.title = detail_title or second.title
                second.year = detail_year or second.year
                return second
        second.status = "low_confidence"
        second.need_confirm = True
        if not ai_passed:
            second.error = (
                f"AI 结构化结果置信度 {ai_result.confidence:.0%} "
                f"低于阈值 {threshold:.0%}，需人工确认"
            )
        elif detail_revalidation_eligible and not detail_available:
            second.error = "AI 修正后的 TMDB 候选未通过详情复核，需人工确认"
        elif position_validation is not None and not position_validation.get("passed"):
            second.error = _tmdb_position_error(position_validation)
        elif not source_year_verified:
            second.error = "AI 修正年份与原始文件年份冲突，需人工确认"
        elif not local_strict_passed:
            second.error = "AI 修正后的 TMDB 候选未达到严格匹配阈值，需人工确认"
        else:
            second.error = "AI 修正后的 TMDB 候选未通过详情复核，需人工确认"
        return second

    def _run_assisted_fallbacks(
        self,
        deterministic: RecognitionResult,
        *,
        processed,
        media_type_hint: str,
        source_anchors: list[str],
    ) -> MatchResult:
        """按固定优先级执行外部线索、发布组知识与 AI 回退。"""
        rejected = set(deterministic.rejected_constraints or [])
        if any(item.startswith("tmdb_position_") for item in rejected):
            return self._attach_preprocess(deterministic, processed)
        external = self._external_hint_fallback(
            processed.filename,
            processed.parent_path,
            deterministic,
            source_anchors=source_anchors,
        )
        if external.status == "matched":
            return self._attach_preprocess(external, processed)
        if "ambiguous_near_tie" in rejected:
            # 近似同名只允许“外部标题线索 + TMDB 二次严格校验”解除；发布组
            # 学习、普通 LLM 或 Web 搜索不得在外部线索无效时继续覆盖近似冲突。
            return self._attach_preprocess(deterministic, processed)
        learned = self._release_group_fallback(
            processed.filename,
            processed.parent_path,
            external,
            media_type_hint=media_type_hint,
        )
        if learned.status == "matched":
            if learned.context is not None:
                learned.context.season = processed.season
                learned.context.episode = processed.episode
                if processed.season is not None or processed.episode is not None:
                    learned.context.media_type = "tv"
            return self._attach_preprocess(learned, processed)
        final = self._ai_fallback(
            processed.filename,
            processed.parent_path,
            learned,
            source_anchors=source_anchors,
        )
        return self._attach_preprocess(final, processed)

    # ===== 主匹配 =====
    def match(
        self, filename: str, parent_path: str = "", *, media_type_hint: str = ""
    ) -> MatchResult:
        self._last_search_error = ""
        self._last_search_status = ""
        parsed = self._parse_filename_fields(filename)
        filename_tmdb_id, filename_tmdb_conflict = _resolve_explicit_tmdb_marker(
            filename
        )
        inherited_tmdb_id = ""
        parent_tmdb_conflict = False
        if not filename_tmdb_conflict and not filename_tmdb_id:
            inherited_tmdb_id, parent_tmdb_conflict = _resolve_explicit_tmdb_marker(
                parent_path, nearest_first=True
            )
        parsed["tmdb_id"] = filename_tmdb_id or inherited_tmdb_id
        hint = str(media_type_hint or "").strip().lower()
        if hint not in {"movie", "tv"}:
            hint = ""

        # 辅助源最终放行必须回看未经过管理员预处理规则的标题锚点；搜索本身
        # 仍使用预处理投影，以保留规则对召回率的改善。
        raw_context = extract_recognition_context(filename, parent_path)
        raw_source_anchors = _source_title_anchors(raw_context)

        # 先在原始输入上解析投影，但不改变显式 ID、人工锁和管理员规则的优先级。
        processed = self.prepare_recognition(filename, parent_path)

        if filename_tmdb_conflict or parent_tmdb_conflict:
            result = MatchResult(
                media_type=hint or parsed.get("type") or "",
                need_confirm=True, status="low_confidence",
                matched_by="tmdb_marker_conflict", threshold=1.0,
                error="同一路径层级包含多个不同 TMDB 标记，已阻止自动整理",
            )
            return self._attach_preprocess(result, processed)

        # 用户已标注 TMDB ID 时直接读取详情。父目录标记还需要结合目录中的
        # “第二季 / Season 02 / S02”上下文判断 TV，避免仅以 ``13.mp4``
        # 这类通用文件名误走 movie 详情端点。
        if parsed.get("tmdb_id"):
            explicit_media_type = hint or parsed.get("type") or "movie"
            if inherited_tmdb_id and not hint and explicit_media_type != "tv":
                if raw_context.media_type == "tv":
                    explicit_media_type = "tv"
            if (
                inherited_tmdb_id
                and not hint
                and raw_context.season is None
                and raw_context.episode is None
            ):
                result = self._match_inherited_tmdb(
                    parsed["tmdb_id"], explicit_media_type, raw_context
                )
            else:
                result = self.match_from_tmdb(
                    parsed["tmdb_id"], explicit_media_type
                )
            return self._attach_preprocess(result, processed)
        media_type = hint or parsed["type"]

        # 1. 上下文映射锁。旧版仅按文件名保存的锁保留审计但不再自动命中。
        locked = self._get_lock(filename, parent_path, media_type_hint=hint)
        if locked:
            result = MatchResult(
                tmdb_id=locked["tmdb_id"], title=locked.get("title", ""),
                year=locked.get("year", ""), media_type=locked.get("media_type") or media_type,
                confidence=1.0, locked=True, status="matched", matched_by="lock",
                threshold=1.0, provider="tmdb",
                external_id=str(locked.get("tmdb_id") or ""),
            )
            return self._attach_preprocess(result, processed)

        # 显式 ID 与人工锁之后、普通 TMDB 搜索之前应用管理员规则。
        from app.modules.tmdb_regex_rules import find_tmdb_regex_match

        rule_match = find_tmdb_regex_match(filename, parent_path, media_type)
        if rule_match:
            result = self.match_from_tmdb(rule_match.tmdb_id, rule_match.media_type)
            result.locked = False
            result.matched_by = "regex_rule"
            result.regex_rule_id = rule_match.rule_id
            result.season_override = rule_match.season_override
            logger.info(
                f"  ✓ 正则规则匹配: rule={rule_match.rule_id} "
                f"tmdb={rule_match.tmdb_id} type={rule_match.media_type}"
            )
            return self._attach_preprocess(result, processed)

        # 人工确认形成的标题别名只在证据唯一时生效，并且仍回读 TMDB
        # 详情；这比继续扩张发布组硬编码更稳定，也不会绕过类型校验。
        try:
            from app.modules.media_aliases import lookup_manual_alias

            alias_context = extract_recognition_context(
                processed.filename, processed.parent_path
            )
            alias_entry = lookup_manual_alias(
                _unique_text((
                    alias_context.normalized_title,
                    alias_context.filename_title,
                    alias_context.folder_title,
                    *alias_context.title_variants,
                )),
                media_type=media_type,
                year=alias_context.filename_year or alias_context.folder_year,
            )
        except Exception as exc:
            logger.debug("人工媒体别名查询失败 type=%s", type(exc).__name__)
            alias_entry = None
        if alias_entry:
            result = self.match_from_tmdb(alias_entry["tmdb_id"], media_type)
            if result.status == "matched":
                result.matched_by = "manual_alias"
                result.locked = True
                result.metadata = {
                    **dict(result.metadata or {}),
                    "manual_alias": str(alias_entry.get("alias") or ""),
                }
                return self._attach_preprocess(result, processed)

        # 文件名和父目录属于外部输入。若其中混入真实凭据，禁止把标题投影
        # 发送给 TMDB、辅助搜索或 AI；显式 TMDB ID、人工锁、管理员规则和
        # 已确认别名只按 ID 回读详情，已在此前安全处理。
        if contains_sensitive_credential(filename) or contains_sensitive_credential(parent_path):
            result = MatchResult(
                media_type=media_type,
                need_confirm=True,
                status="low_confidence",
                matched_by="sensitive_source",
                threshold=1.0,
                error="原始文件名或目录包含疑似凭据，已阻止联网标题识别，请人工确认",
            )
            return self._attach_preprocess(result, processed)

        deterministic = (
            self.deterministic_recognize(
                processed.filename, processed.parent_path,
                media_type_hint=hint, source_context=raw_context,
            )
            if hint
            else self.deterministic_recognize(
                processed.filename, processed.parent_path,
                source_context=raw_context,
            )
        )
        if deterministic.context is not None:
            verified_implicit_season = bool(
                deterministic.matched_by == "implicit_season_fallback"
                and deterministic.season_override is not None
                and (deterministic.metadata or {}).get("implicit_season_fallback")
            )
            if not verified_implicit_season:
                deterministic.context.season = processed.season
                deterministic.context.episode = processed.episode
            if (
                verified_implicit_season
                or processed.season is not None
                or processed.episode is not None
            ):
                deterministic.context.media_type = "tv"
        if (
            deterministic.status == "matched"
            or verified_automatic_identity_proof(deterministic) is not None
        ):
            return self._attach_preprocess(deterministic, processed)
        return self._run_assisted_fallbacks(
            deterministic,
            processed=processed,
            media_type_hint=hint,
            source_anchors=raw_source_anchors,
        )

    def _pick_best(self, clean_title: str, year: str, media_type: str,
                   candidates: list[dict]) -> MatchResult:
        scored: list[tuple[float, dict, str, str, float]] = []
        for c in candidates:
            c_title = c.get("name") or c.get("title", "")
            c_original = c.get("original_name") or c.get("original_title", "") or c_title
            c_year = (c.get("first_air_date", "") or c.get("release_date", ""))[:4]
            # 比对中文名 + 原名，取最高（解决英文标题 vs 中文译名失效）
            sim = max(
                self._title_similarity(clean_title, self.clean_title(c_title)),
                self._title_similarity(clean_title, self.clean_title(c_original)),
            )
            # 有年份时标题/年份按 0.7/0.3；无年份时不凭空扣 15% 置信度。
            if year:
                ym = 1.0 if c_year == year else 0.0
                score = sim * 0.7 + ym * 0.3
            else:
                ym = 0.0
                score = sim
            scored.append((score, c, c_title, c_year, sim))
        scored.sort(key=lambda x: x[0], reverse=True)

        best_score, best_c, best_title, best_year, _ = scored[0]
        top3 = [
            Candidate(
                tmdb_id=str(c.get("id", "")), title=t, year=y,
                score=round(s, 3), media_type=media_type,
                original_title=str(c.get("original_name") or c.get("original_title") or ""),
                overview=str(c.get("overview") or ""),
                poster_path=str(c.get("poster_path") or ""),
                backdrop_path=str(c.get("backdrop_path") or ""),
                release_date=str(c.get("first_air_date") or c.get("release_date") or ""),
                provider="tmdb",
                external_id=str(c.get("id") or ""),
                metadata={
                    "genre_ids": [
                        int(value) for value in (c.get("genre_ids") or [])
                        if str(value).isdigit()
                    ],
                },
            )
            for s, c, t, y, _ in scored[:3]
        ]
        return MatchResult(
            tmdb_id=str(best_c.get("id", "")),
            title=best_title, year=best_year, media_type=media_type,
            confidence=round(best_score, 3), candidates=top3,
            provider="tmdb", external_id=str(best_c.get("id") or ""),
        )

    # ===== 映射锁 =====
    @staticmethod
    def _lock_context(
        raw_name: str,
        parent_path: str = "",
        media_type_hint: str = "",
    ) -> tuple[str, str, int]:
        normalized_parent = re.sub(
            r"/+", "/", str(parent_path or "").replace("\\", "/").strip()
        ).strip("/")
        context = extract_recognition_context(raw_name, normalized_parent)
        hint = str(media_type_hint or "").strip().lower()
        media_type = hint if hint in {"movie", "tv"} else context.media_type
        season = context.season if isinstance(context.season, int) and context.season >= 0 else -1
        return normalized_parent, media_type, season

    def _get_lock(
        self,
        raw_name: str,
        parent_path: str = "",
        *,
        media_type_hint: str = "",
    ) -> Optional[dict]:
        normalized_parent, media_type, season = self._lock_context(
            raw_name, parent_path, media_type_hint
        )
        return get_tmdb_lock(
            raw_name=raw_name,
            parent_path=normalized_parent,
            media_type=media_type,
            season=season,
        )

    def _set_lock(self, raw_name: str, tmdb_id: str, title: str, year: str,
                  media_type: str = "", lock_source: str = "manual",
                  parent_path: str = "") -> None:
        source = "manual" if lock_source == "manual" else "automatic"
        normalized_parent, normalized_type, season = self._lock_context(
            raw_name, parent_path, media_type
        )
        upsert_tmdb_lock(
            raw_name=raw_name,
            parent_path=normalized_parent,
            tmdb_id=tmdb_id,
            title=title,
            year=year,
            media_type=normalized_type,
            season=season,
            lock_source=source,
        )

    def confirm(self, raw_name: str, tmdb_id: str, title: str,
                year: str, media_type: str = "", parent_path: str = "",
                rejected_tmdb_ids: list[str] | None = None) -> None:
        """人工确认后按文件名、父目录、类型与季号锁定映射。"""
        self._set_lock(
            raw_name, tmdb_id, title, year, media_type, parent_path=parent_path
        )
        try:
            from app.modules.media_aliases import record_manual_confirmation

            context = extract_recognition_context(raw_name, parent_path)
            alias_type = media_type if media_type in {"movie", "tv"} else context.media_type
            record_manual_confirmation(
                _unique_text((
                    context.normalized_title,
                    context.filename_title,
                    context.folder_title,
                    *context.title_variants,
                    title,
                )),
                tmdb_id=tmdb_id,
                title=title,
                year=year,
                media_type=alias_type,
                rejected_tmdb_ids=rejected_tmdb_ids or (),
            )
        except Exception as exc:
            # 映射锁是人工确认的主契约；别名学习失败不得让确认操作失败。
            logger.warning("人工媒体别名记录失败 type=%s", type(exc).__name__)

        # 人工确认也是发布组知识最可靠的证据之一。只按“作品 + 物理目录”
        # 生成样本键，同一合集中的多集确认只算一个样本；来自另一目录的
        # 独立确认才会让候选发布组达到自动启用门槛。学习失败不得影响主确认。
        try:
            from app.modules.recognition_knowledge import record_learned_release_group

            context = extract_recognition_context(raw_name, parent_path)
            release_groups = _unique_text(
                context.cleaned_components.get("candidate_release_groups", [])
            )
            normalized_parent = re.sub(
                r"/+", "/", unicodedata.normalize(
                    "NFKC", str(parent_path or "").replace("\\", "/")
                ).strip(),
            ).strip("/")
            package_identity = (
                normalized_parent
                or context.folder_title
                or context.normalized_title
                or title
            )
            for group in release_groups:
                sample_key = hashlib.sha256(
                    f"manual\0{group}\0{tmdb_id}\0{package_identity}".encode("utf-8")
                ).hexdigest()[:32]
                record_learned_release_group(
                    group,
                    confidence=1.0,
                    evidence={
                        "sample_key": sample_key,
                        "source": "manual_confirmation",
                        "tmdb_id": str(tmdb_id),
                        "title": str(title or ""),
                        "year": str(year or ""),
                        "media_type": str(media_type or context.media_type or ""),
                        "package_identity": package_identity,
                    },
                )
        except Exception as exc:
            logger.warning("人工发布组知识记录失败 type=%s", type(exc).__name__)
        logger.info(
            "已锁定映射: %s [%s] → tmdb-%s (%s)",
            raw_name, parent_path or "根目录", tmdb_id, title,
        )


def deterministic_recognize(filename: str, parent_path: str = "") -> RecognitionResult:
    """兼容函数入口；现有调用只传 filename 时仍可工作。"""
    scraper = TMDBScraper()
    try:
        return scraper.deterministic_recognize(filename, parent_path)
    finally:
        scraper.close()
