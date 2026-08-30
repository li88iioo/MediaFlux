"""网盘整理模块。

流程：扫描源目录 → 刮削识别 → 分类归档 → 命名标准化 → 冲突覆盖 → 移动 → 日志。
默认 dry_run=True 只输出整理计划不实际移动（安全验证）。

分类策略（按需求文档）：
- 主类：电影 / 剧集 / 动漫 / 纪录片 / 综艺（可选儿童节目/演唱会）
- 地区二次分类：国产 / 欧美 / 日韩 / 其他
- 年份三次分类
覆盖策略：Remux/蓝光优先、大分辨率优先、杜比优先、冲突三档（1不覆盖/2大优先/3小优先）
"""
from __future__ import annotations

import copy
from decimal import Decimal
import json
import logging
import re
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from difflib import SequenceMatcher
from typing import Callable

from app import database as db
from app.clients.guangya import GuangYaClient, GuangYaFile, close_guangya_client
from app.config import get, get_bool, get_int
from app.database import add_organize_log, add_organize_log_items, get_media_probe_cache
from app.logger import get_logger, log_throttled
from app.modules.media_variant import MediaVariant, classify_variant, variants_can_coexist
from app.modules.recognition_policy import (
    automatic_match_confirmation_message,
    automatic_match_policy,
    normalize_automatic_match_preset,
)
from app.modules.episode_mapping import (
    DirectoryEpisodeEvidence,
    EpisodeMappingPlan,
    build_directory_episode_evidence,
    infer_episode_mapping,
    infer_merged_season_cour_mapping,
    season_episode_counts,
)
from app.modules.directory_scrape_errors import (
    DirectoryScrapeConflictError,
    DirectoryScrapePublicError,
    DirectoryScrapeStateError,
    public_error_message,
)
from app.modules.naming import (
    MOVIE_DEFAULT,
    MOVIE_DIR_DEFAULT,
    SHOW_DIR_DEFAULT,
    TV_DEFAULT,
    append_variant_tags,
    build_context,
    render_template,
    template_has_media_identity,
)
from app.modules.scraper import (
    MatchResult,
    TMDBScraper,
    _explicit_tmdb_id_from_path,
    _has_explicit_tmdb_marker,
    _resolve_explicit_tmdb_marker,
    extract_recognition_context,
    has_unresolved_candidate_title_remainder,
    has_unresolved_season_hint,
    implicit_season_conflicts_with_candidate_title,
    infer_tmdb_season_from_title_evidence,
    parse_release_position,
    verified_automatic_identity_proof,
)
from app.modules.subtitle_identity import plan_subtitle_companions
from app.modules.special_media import (
    fixed_special_media_position,
    fractional_episode_position,
    has_fractional_episode_position,
    is_special_directory_name,
    is_special_media_name,
    special_media_position,
    strip_special_media_markers,
    title_hint_from_path,
)
from app.modules.organize_scan import (
    DEFAULT_TRAVERSAL_MAX_DEPTH,
    DEFAULT_TRAVERSAL_MAX_DIRS,
    DEFAULT_TRAVERSAL_MAX_ENTRIES,
    OrganizerScanner,
    OrganizeScanResult,
    ScanRestriction,
    _ScannedVideo,
    _TraversalBudget,
    _TraversalLimitExceeded,
)
from app.modules.organize_groups import (
    GROUP_ROOT_PATH,
    GROUP_STAGE_CLEANUP,
    GROUP_STAGE_DONE,
    GROUP_STAGE_EXECUTE,
    GROUP_STAGE_PENDING,
    GROUP_STAGE_PLAN,
    GROUP_STAGE_SCAN,
    GROUP_STATUS_RUNNING,
    GROUP_STATUS_STOPPED,
    GroupProgress,
    OrganizeGroupResult,
    OrganizeGroupTask,
    build_group_result,
    enumerate_group_tasks,
    is_terminal_group_status,
    merge_group_stats,
)
from app.modules.organize_execution import execute_organize_plans
from app.modules.organize_postprocess import (
    media_role,
    normalize_media_number,
    normalized_stem,
)
from app.modules.organize_delete_audit import (
    DeleteCandidate,
    execute_recycle_bin_delete,
    record_blocked_delete,
)

logger = get_logger(__name__)

# TMDB genre id
GENRE_ANIME = 16
GENRE_DOC = 99
GENRE_VARIETY_REALITY = 10764
GENRE_VARIETY_TALK = 10763
GENRE_KIDS = 10762
GENRE_FAMILY = 10751
GENRE_MUSIC = 10402

CONCERT_RE = re.compile(r"(?i)(演唱会|音乐会|巡回演出|concert|live\s+(?:at|in|from)|world\s+tour)")
KIDS_RE = re.compile(r"(?i)(儿童|少儿|幼儿|kids?|children)")

DEFAULT_ORGANIZE_VIDEO_EXTS = (
    "mkv", "mp4", "ts", "m2ts", "mts", "avi", "mov", "m4v", "webm",
    "mpeg", "mpg", "wmv", "flv", "vob", "tp", "f4v", "rm", "rmvb",
)
DEFAULT_ORGANIZE_METADATA_EXTS = (
    "nfo", "srt", "ass", "ssa", "sup", "vtt", "sub", "idx",
    "jpg", "jpeg", "png", "webp",
)
VIDEO_EXTS = set(DEFAULT_ORGANIZE_VIDEO_EXTS)
METADATA_EXTS = set(DEFAULT_ORGANIZE_METADATA_EXTS)

REGION_MAP = {
    "CN": "国产", "HK": "国产", "TW": "国产", "MO": "国产",
    "US": "欧美", "GB": "欧美", "IE": "欧美", "FR": "欧美", "DE": "欧美",
    "CA": "欧美", "AU": "欧美", "NZ": "欧美", "IT": "欧美", "ES": "欧美",
    "PT": "欧美", "NL": "欧美", "BE": "欧美", "AT": "欧美", "CH": "欧美",
    "SE": "欧美", "NO": "欧美", "DK": "欧美", "FI": "欧美", "IS": "欧美",
    "JP": "日韩", "KR": "日韩",
}

SAFE_RE = re.compile(r'[\\/:*?"<>|]')
_MEDIA_IDENTITY_SEPARATORS_RE = re.compile(
    r"[^a-z0-9\u3040-\u30ff\u3400-\u9fff"
    r"\u1100-\u11ff\u3130-\u318f\ua960-\ua97f\uac00-\ud7a3\ud7b0-\ud7ff]+"
)
_DIRECTORY_TITLE_SEGMENT_RE = re.compile(
    r"[\[【(（]([^\]】)）]{1,160})[\]】)）]"
)
_MEDIA_IDENTITY_TOKEN_RE = re.compile(
    r"[a-z0-9]+|[\u3040-\u30ff]+|[\u3400-\u9fff]+|"
    r"[\u1100-\u11ff\u3130-\u318f\ua960-\ua97f\uac00-\ud7a3\ud7b0-\ud7ff]+",
    re.IGNORECASE,
)


def _normalize_media_identity(value: object) -> str:
    """生成跨目录缓存与剧集证据共用的稳定媒体标题身份。"""
    normalized = unicodedata.normalize("NFC", str(value or ""))
    return _MEDIA_IDENTITY_SEPARATORS_RE.sub("", normalized.casefold())


_GENERIC_FILENAME_IDENTITY_HINTS = {
    "anime", "episode", "episodes", "ep", "file", "movie", "season",
    "show", "tv", "unknown", "video",
}
_SPECIAL_FILENAME_IDENTITY_MARKER_RE = re.compile(
    r"(?ix)(?<![a-z0-9])(?:"
    r"s\d{1,3}[ ._-]*e(?:p)?[ ._-]*0{1,3}|"
    r"s0{1,3}[ ._-]*e(?:p)?[ ._-]*\d{1,3}|"
    r"nc(?:op|ed)(?:[ ._-]*\d{1,3})?|"
    r"(?:ova|oav|oad|specials?|sps?|omnibus)(?:[ ._-]*\d{1,3})?|"
    r"(?:op|ed)(?:[ ._-]*\d{1,3})?"
    r")(?![a-z0-9])"
)


def _usable_filename_identity_hint(filename: str) -> str:
    """提取可支撑目录级连续剧集包识别的文件名标题。

    目录名常混入发布组、编码组或打包者标签。只有目录已经形成连续剧集
    证据时才会调用本函数，并且极短、纯数字和通用占位标题都会失败关闭，
    回退到原有路径标题逻辑。
    """
    context = extract_recognition_context(str(filename or ""), "")
    title = str(context.filename_title or context.normalized_title or "").strip()
    identity = _normalize_media_identity(title)
    if (
        not title
        or len(identity) < 4
        or identity.isdigit()
        or identity in _GENERIC_FILENAME_IDENTITY_HINTS
    ):
        return ""
    return title


def _special_filename_identity_hint(filename: str) -> str:
    """提取特殊集文件自身携带的作品标题，失败时由调用方回退父目录。

    ``S00E01``、``OVA``、``NCOP`` 等词只描述特殊集位置，不属于作品名。
    仅当文件名本身明确带有特殊集标记时调用本函数；去掉标记后若只剩
    空值或通用词则失败关闭，避免把裸 ``NCOP.mkv`` 当作作品标题搜索。
    """
    if not is_special_media_name(filename):
        return ""
    stem = str(filename or "").rsplit("/", 1)[-1]
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    cleaned = strip_special_media_markers(stem)
    cleaned = _SPECIAL_FILENAME_IDENTITY_MARKER_RE.sub(" ", cleaned)
    cleaned = re.sub(r"[ ._\-]+", " ", cleaned).strip(" []()【】._-")
    if not cleaned:
        return ""
    return _usable_filename_identity_hint(f"{cleaned}.mkv")


def _recognition_identity_year(filename: str, parent_context: str) -> str:
    """返回来源文件/目录中经解析器确认的可靠年份。

    目录级剧集包会把多个文件折叠成同一个身份识别请求；该请求必须保留
    ``2008`` 之类的作品/季年份，否则精确官方别名可能重新落入多候选歧义。
    这里只接受解析器产出的 19xx/20xx 四位年份，不直接扫描原始数字，避免
    把 1080、2160、集号或版本号带入识别标题。
    """
    context = extract_recognition_context(filename, parent_context)
    for value in (context.filename_year, context.folder_year):
        year = str(value or "").strip()
        if re.fullmatch(r"(?:19|20)\d{2}", year):
            return year
    return ""


def _directory_episode_identity_hint(filename: str, parent_context: str) -> str:
    """为连续剧集包选择与短文件标题兼容的更完整目录标题。

    例如文件只有 ``Boruto - 001``，而目录包含
    ``(Boruto: Naruto Next Generations)``。只有目录候选是文件标题的严格、
    有信息量扩展时才采用；否则保持文件名标题，避免无关父目录污染识别。
    """
    file_title = _usable_filename_identity_hint(filename)
    if not file_title:
        return ""
    file_identity = _normalize_media_identity(file_title)
    file_tokens = _MEDIA_IDENTITY_TOKEN_RE.findall(
        unicodedata.normalize("NFKC", file_title).casefold()
    )

    context = extract_recognition_context(filename, parent_context)
    raw_candidates = [str(context.folder_title or "").strip()]
    raw_candidates.extend(
        match.group(1).strip()
        for match in _DIRECTORY_TITLE_SEGMENT_RE.finditer(str(parent_context or ""))
    )

    compatible: list[tuple[int, str]] = []
    seen: set[str] = set()
    for raw_candidate in raw_candidates:
        if not raw_candidate:
            continue
        parsed = extract_recognition_context(
            f"{raw_candidate}.S01E01.mkv", ""
        )
        candidate = str(
            parsed.filename_title or parsed.normalized_title or raw_candidate
        ).strip()
        candidate_identity = _normalize_media_identity(candidate)
        if (
            not candidate
            or candidate_identity in seen
            or candidate_identity == file_identity
            or not candidate_identity.startswith(file_identity)
        ):
            continue
        seen.add(candidate_identity)
        extra_identity = candidate_identity[len(file_identity):]
        if len(extra_identity) < 6:
            continue

        candidate_tokens = _MEDIA_IDENTITY_TOKEN_RE.findall(
            unicodedata.normalize("NFKC", candidate).casefold()
        )
        token_extension = bool(
            file_tokens
            and candidate_tokens[:len(file_tokens)] == file_tokens
            and len(candidate_tokens) >= len(file_tokens) + 2
        )
        cjk_extension = bool(
            re.search(r"[\u3040-\u30ff\u3400-\u9fff]", file_title)
            and len(extra_identity) >= 6
        )
        if token_extension or cjk_extension:
            compatible.append((len(candidate_identity), candidate))

    if not compatible:
        return file_title
    compatible.sort(key=lambda item: (item[0], item[1].casefold()))
    return compatible[0][1]


_ORGANIZE_FAILURE_MESSAGE = "文件整理失败，请稍后重试"

_DIRECTORY_PACKAGE_IDENTITY_PROOF_KEY = "verified_directory_package_identity_proof"
_DIRECTORY_PACKAGE_IDENTITY_ACCEPTED_KEY = (
    "verified_directory_package_identity_proof_accepted"
)
_DIRECTORY_IDENTITY_ATTESTATION_ACCEPTED_KEY = (
    "verified_directory_identity_attestation_accepted"
)
_DIRECTORY_IDENTITY_ATTESTATION_VERSION = 1
_DIRECTORY_PACKAGE_IDENTITY_PROOF_VERSION = 2
_DIRECTORY_PACKAGE_IDENTITY_MIN_CONFIDENCE = 0.82
_DIRECTORY_PACKAGE_IDENTITY_MIN_EPISODES = 12
_DIRECTORY_PACKAGE_IDENTITY_MIN_BREAKDOWN_SCORE = 0.80
_DIRECTORY_PACKAGE_IDENTITY_MIN_FILE_ANCHOR_SCORE = 0.82
_DIRECTORY_PACKAGE_IDENTITY_MIN_FOLDER_ANCHOR_SCORE = 0.64


def automatic_match_requires_confirmation(
    match: MatchResult | None, *, threshold: float = 0.9,
) -> bool:
    """统一判定无人工选择的自动整理结果是否足够安全。

    默认均衡档保持原有 90% 严格门槛。积极档只放宽“唯一 TMDB 候选因
    分数略低于 strict 阈值”这一种情况；类型/年份/季集冲突、近似并列、
    AI 未复核结果以及缺少结构化评分证据时仍失败关闭。
    """
    if match is None:
        return True
    try:
        required = float(threshold)
        confidence = float(getattr(match, "confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        return True
    if not 0.0 < required <= 1.0 or confidence < required:
        return True

    status = str(getattr(match, "status", "") or "").strip().lower()
    need_confirm = bool(getattr(match, "need_confirm", False))
    if (not status or status == "matched") and not need_confirm:
        return False

    # 90% 及以上档位绝不覆盖识别器的显式人工确认结论。
    if required >= 0.9:
        return True
    if status not in {"low_confidence", "matched"}:
        return True
    if str(getattr(match, "provider", "") or "").strip().lower() != "tmdb":
        return True
    matched_by = str(getattr(match, "matched_by", "") or "").strip().lower()
    if matched_by and matched_by not in {"search", "title_search"}:
        return True
    if dict(getattr(match, "ai_diagnostic", None) or {}):
        return True
    if list(getattr(match, "rejected_constraints", None) or []):
        return True

    decision = dict(getattr(match, "threshold_decision", None) or {})
    if str(decision.get("reason") or "") != "below_threshold":
        return True
    try:
        if abs(float(decision.get("score")) - confidence) > 0.001:
            return True
    except (TypeError, ValueError):
        return True

    candidates = list(getattr(match, "candidates", None) or [])
    if not candidates:
        return True
    selected = getattr(candidates[0], "score_breakdown", None)
    if selected is None or list(getattr(selected, "rejected_constraints", None) or []):
        return True
    strong_title_score = max(
        float(getattr(selected, "title_score", 0.0) or 0.0),
        float(getattr(selected, "original_title_score", 0.0) or 0.0),
        float(getattr(selected, "alias_score", 0.0) or 0.0),
    )
    if strong_title_score < required:
        return True
    if len(candidates) > 1:
        second_score = float(getattr(candidates[1], "score", 0.0) or 0.0)
        if confidence - second_score < 0.08:
            return True
    return False


def _safe_organize_failure(exc: Exception) -> str:
    if isinstance(exc, DirectoryScrapePublicError):
        return public_error_message(exc)
    return _ORGANIZE_FAILURE_MESSAGE


def _format_scan_summary(stats: dict) -> str:
    """把扫描统计压成一行可读摘要。

    完整统计已经作为结构化结果返回给 Web/TG，日志只保留人能一眼看懂的
    关键项：为零的计数不打印，异常项始终打印。
    """
    stats = stats if isinstance(stats, dict) else {}

    def count(key: str) -> int:
        try:
            return int(stats.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    groups = [item for item in (stats.get("source_groups") or []) if isinstance(item, dict)]
    scope = ""
    if len(groups) == 1:
        scope = str(groups[0].get("name") or groups[0].get("path") or "")
    elif groups:
        scope = f"{len(groups)} 个媒体目录"

    parts = [f"共 {count('total')} 个视频"]
    for label, key in (
        ("已识别", "matched"), ("待确认", "need_confirm"), ("跳过", "skipped"),
        ("冲突", "conflict"), ("失败", "failed"), ("已移动", "moved"),
        ("伴随文件", "metadata_moved"), ("特别篇", "specials_auto_mapped"),
    ):
        value = count(key)
        if value:
            parts.append(f"{label} {value}")
    if count("stopped"):
        parts.append("已停止")
    if not stats.get("scan_complete", True) or count("scan_limited"):
        parts.append(f"扫描不完整({stats.get('scan_limit_kind') or '未知原因'})")
    errors = stats.get("scan_errors") or []
    if errors:
        parts.append(f"扫描错误 {len(errors)}")

    timing = " ".join(
        f"{label}={float(stats.get(key) or 0.0):.2f}s"
        for label, key in (
            ("扫描", "scan_elapsed_seconds"),
            ("识别", "recognition_elapsed_seconds"),
            ("探测", "media_probe_elapsed_seconds"),
            ("冲突", "conflict_check_elapsed_seconds"),
        )
        if float(stats.get(key) or 0.0) > 0
    )
    summary = f"[{scope}] " if scope else ""
    summary += " · ".join(parts)
    return f"{summary} | {timing}" if timing else summary


def _format_phase_timing(stats: dict) -> str:
    """输出阶段耗时与外部请求量；为零的诊断项不打印。"""
    stats = stats if isinstance(stats, dict) else {}
    chunks = []
    for label, key in (
        ("scan", "scan_elapsed_seconds"),
        ("recognition", "recognition_elapsed_seconds"),
        ("probe", "media_probe_elapsed_seconds"),
        ("conflict", "conflict_check_elapsed_seconds"),
        ("execute", "execute_elapsed_seconds"),
        ("cleanup", "cleanup_elapsed_seconds"),
    ):
        value = float(stats.get(key) or 0.0)
        if value > 0:
            chunks.append(f"{label}={value:.2f}s")
    chunks.append(f"total={float(stats.get('total_elapsed_seconds') or 0.0):.2f}s")

    for label, key in (
        ("tmdb", "tmdb_search_requests"),
        ("tmdb_cache", "tmdb_search_cache_hits"),
        ("ai", "ai_requests"),
        ("list_dir", "scan_list_dir_calls"),
        ("probe_cache", "media_probe_cache_hits"),
        ("probe_online", "media_probe_online_profiles"),
        ("probe_timeouts", "media_probe_timeouts"),
        ("target_refresh", "target_dir_refreshes"),
    ):
        try:
            value = int(stats.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value:
            chunks.append(f"{label}={value}")
    return " ".join(chunks)


# 媒体组流水线的来源级探测墙钟上限：每组各有独立预算，防止病态来源
# 按组数把在线探测放大成小时级任务；到达上限后剩余组只读探测缓存。
_GROUP_PIPELINE_PROBE_CAP_SECONDS = 300.0


class OrganizeScanUnsafeError(RuntimeError):
    """扫描快照不可信，整个来源必须失败关闭。

    这类错误与单个媒体组的运行期失败不同：部分快照会让后续清理和冲突
    仲裁失去依据，因此禁止被组级失败隔离吞掉。
    """


class _OrganizeAuditWriteError(RuntimeError):
    def __init__(self, log_id: int, cause: Exception):
        super().__init__(str(cause))
        self.log_id = int(log_id)
        self.__cause__ = cause


@dataclass
class OrganizePlan:
    file_id: str
    original_name: str
    original_path: str
    original_parent_id: str = "0"
    size: int = 0
    etag: str = ""
    match: MatchResult = None
    main_category: str = ""
    region: str = ""
    year: str = ""
    season: int | None = None
    episode: int | None = None
    source_season: int | None = None
    source_episode: int | None = None
    episode_mapping: EpisodeMappingPlan | None = None
    base_name: str = ""
    new_name: str = ""
    variant: MediaVariant = field(default_factory=MediaVariant)
    variant_label: str = ""
    variant_suffix: str = ""
    conflict_decision: str = "new"
    conflict_note: str = ""
    target_path: str = ""
    media_root_path: str = ""
    identity_guard_required: bool = False
    backdrop_path: str = ""
    poster_path: str = ""
    season_total: int = 0
    action: str = "move"  # move / skip / conflict
    note: str = ""
    # 追加在末尾以保持历史位置参数构造的语义兼容。
    source_group_id: str = ""
    source_group_path: str = ""
    media_profile: object | None = field(default=None, repr=False, compare=False)
    media_probe_complete: bool = False
    media_probe_pending: bool = False
    conflict_existing_id: str = ""
    conflict_existing_name: str = ""
    multipart_index: int | None = None
    multipart_token: str = ""
    multipart_ambiguous: bool = False


@dataclass(frozen=True)
class OrganizeContext:
    """一次整理运行的上下文。

    业务规则由 :class:`OrganizeRules` 承载；这里仅保存运行时控制项，
    避免内部阶段继续传递一长串容易错位的参数。
    """

    source_dir_id: str
    dry_run: bool = True
    max_files: int = 0
    cancel_event: threading.Event | None = None
    post_actions: bool = True
    source_name: str = ""
    require_complete_scan: bool = False
    media_probe_cache_only: bool | None = None
    protected_source_ids: frozenset[str] = frozenset()
    automatic: bool = False
    # 组级流水线的实时进度回调；只用于观测，异常不得影响整理结果。
    group_progress: Callable[[dict], None] | None = None
    # 回退开关：为 False 时继续使用整源扫描/规划/执行的旧路径。
    group_pipeline: bool = True
    # 单次调用的审计归属键；用于精确回读本轮日志，避免并发任务污染。
    operation_token: str = ""
    # 来源适配器可提供显式媒体类型提示；普通光鸭整理保持空值，继续依赖
    # 文件名与目录上下文自动判断。本地来源配置和手动刮削可复用同一规划
    # 流水线，而不需要在规划器外再实现一套 match/parse 分支。
    media_type_hint: str = ""

    @property
    def probe_cache_only(self) -> bool:
        if self.media_probe_cache_only is None:
            return self.dry_run
        return bool(self.media_probe_cache_only)

    def cancelled(self) -> bool:
        return bool(self.cancel_event and self.cancel_event.is_set())


@dataclass
class OrganizePlanningResult:
    plans: list[OrganizePlan]
    subtitle_plans_by_video: dict[str, list]


@dataclass
class OrganizeRules:
    target_dir_id: str = "0"
    add_kids: bool = False
    add_concert: bool = False
    region_split: bool = True
    year_split: bool = True
    small_file_mb: int = 10
    clean_empty: bool = True
    conflict_strategy: int = 1  # 1=不覆盖仅同名 2=覆盖大文件优先 3=覆盖小文件优先
    remux_first: bool = True
    resolution_first: bool = True
    dolby_first: bool = True
    keep_multi_versions: bool = False
    keep_remux_variant: bool = False
    recycle_replaced_enabled: bool = False
    link_strm: bool = True
    video_exts: str = ""
    metadata_exts: str = ""
    rename_enabled: bool = True
    media_info_enabled: bool = True
    media_probe_enabled: bool = True
    media_probe_timeout: int = 30
    movie_dir_template: str = MOVIE_DIR_DEFAULT
    movie_template: str = MOVIE_DEFAULT
    tv_template: str = TV_DEFAULT
    show_dir_template: str = SHOW_DIR_DEFAULT
    naming_scope: str = "both"
    notify_enabled: bool = True
    library_notify: bool = True
    strm_detail_notify: bool = True
    emby_refresh: bool = True
    nsfw_enabled: bool = False
    nsfw_source_ids: str = ""
    nsfw_exclusive: bool = False
    nsfw_metatube_endpoint: str = ""
    nsfw_metatube_token: str = ""
    nsfw_category_name: str = "成人内容"
    nsfw_strip_domains: str = ""
    nsfw_timeout_seconds: int = 8
    automatic_match_preset: str = "balanced"

    @classmethod
    def from_config(cls, target_dir_id: str = "") -> "OrganizeRules":
        """读取 Web/TG/自动入库共用的正式整理配置。"""
        return cls(
            target_dir_id=str(target_dir_id or get("GY_ORGANIZE_TARGET_DIR", "0") or "0"),
            add_kids=get_bool("GY_ORGANIZE_ADD_KIDS", False),
            add_concert=get_bool("GY_ORGANIZE_ADD_CONCERT", False),
            region_split=get_bool("GY_ORGANIZE_REGION_SPLIT", True),
            year_split=get_bool("GY_ORGANIZE_YEAR_SPLIT", True),
            small_file_mb=max(0, get_int("GY_ORGANIZE_SMALL_FILE_MB", 10)),
            clean_empty=get_bool("GY_ORGANIZE_CLEAN_EMPTY", True),
            conflict_strategy=max(1, min(get_int("GY_ORGANIZE_CONFLICT_STRATEGY", 1), 3)),
            remux_first=get_bool("GY_ORGANIZE_REMUX_FIRST", True),
            resolution_first=get_bool("GY_ORGANIZE_RESOLUTION_FIRST", True),
            dolby_first=get_bool("GY_ORGANIZE_DOLBY_FIRST", True),
            keep_multi_versions=get_bool("GY_ORGANIZE_KEEP_MULTI_VERSIONS", False),
            keep_remux_variant=get_bool("GY_ORGANIZE_KEEP_REMUX_VARIANT", False),
            recycle_replaced_enabled=get_bool("GY_ORGANIZE_RECYCLE_REPLACED_ENABLED", False),
            link_strm=get_bool("GY_ORGANIZE_LINK_STRM", True),
            video_exts=get("GY_ORGANIZE_VIDEO_EXTS", ""),
            metadata_exts=get("GY_ORGANIZE_METADATA_EXTS", ""),
            # 命名与媒体规格探测已统一为产品固定契约。旧环境变量继续允许
            # 留在 user.env 中，但不再影响任何新任务或历史快照恢复。
            rename_enabled=True,
            media_info_enabled=True,
            media_probe_enabled=True,
            media_probe_timeout=30,
            movie_dir_template=MOVIE_DIR_DEFAULT,
            movie_template=MOVIE_DEFAULT,
            tv_template=TV_DEFAULT,
            show_dir_template=SHOW_DIR_DEFAULT,
            naming_scope="both",
            notify_enabled=get_bool("GY_ORGANIZE_NOTIFY_ENABLED", True),
            library_notify=get_bool("GY_ORGANIZE_LIBRARY_NOTIFY", True),
            strm_detail_notify=get_bool("GY_ORGANIZE_STRM_DETAIL_NOTIFY", True),
            emby_refresh=get_bool("GY_ORGANIZE_EMBY_REFRESH", True),
            nsfw_enabled=get_bool("GY_ORGANIZE_NSFW_ENABLED", False),
            nsfw_source_ids=get("GY_ORGANIZE_NSFW_SOURCE_IDS", ""),
            nsfw_exclusive=False,
            nsfw_metatube_endpoint=get("GY_ORGANIZE_NSFW_METATUBE_ENDPOINT", ""),
            nsfw_metatube_token=get("GY_ORGANIZE_NSFW_METATUBE_TOKEN", ""),
            nsfw_category_name=get("GY_ORGANIZE_NSFW_CATEGORY_NAME", "成人内容") or "成人内容",
            nsfw_strip_domains=get("GY_ORGANIZE_NSFW_STRIP_DOMAINS", ""),
            nsfw_timeout_seconds=max(2, min(get_int("GY_ORGANIZE_NSFW_TIMEOUT_SECONDS", 8), 30)),
            automatic_match_preset=normalize_automatic_match_preset(
                get("GY_ORGANIZE_AUTOMATIC_MATCH_PRESET", "balanced")
            ),
        )

    def selected_nsfw_source_ids(self) -> frozenset[str]:
        """返回已配置的成人专用光鸭来源；异常配置按空集失败关闭。"""
        from app.modules.organize_sources import normalize_organize_source_ids

        source_ids, error = normalize_organize_source_ids(self.nsfw_source_ids)
        if error:
            return frozenset()
        return frozenset(source_ids)

    def for_source(self, source_id: str) -> "OrganizeRules":
        """把全局规则收敛为单个光鸭来源的实际识别边界。

        成人识别只有在来源被显式列入专用范围时才启用；选中的来源只走
        MetaTube 精确番号链，未选来源完全禁用成人识别并保持普通 TMDB 链。
        """
        selected = str(source_id or "").strip() in self.selected_nsfw_source_ids()
        active = bool(self.nsfw_enabled and selected)
        return replace(self, nsfw_enabled=active, nsfw_exclusive=active)

    def for_local_source(self, media_type: str) -> "OrganizeRules":
        """把全局规则收敛为一个本地来源的实际识别边界。"""
        selected = str(media_type or "").strip().lower() == "nsfw"
        active = bool(self.nsfw_enabled and selected)
        return replace(self, nsfw_enabled=active, nsfw_exclusive=active)


def enforce_fixed_organize_rules(rules: OrganizeRules) -> OrganizeRules:
    """覆盖已废弃的命名/探测配置，保证所有入口执行同一整理契约。"""
    return replace(
        rules,
        rename_enabled=True,
        media_info_enabled=True,
        media_probe_enabled=True,
        media_probe_timeout=30,
        movie_dir_template=MOVIE_DIR_DEFAULT,
        movie_template=MOVIE_DEFAULT,
        tv_template=TV_DEFAULT,
        show_dir_template=SHOW_DIR_DEFAULT,
        naming_scope="both",
    )


_ORGANIZE_RULE_SERVER_ONLY_FIELDS = frozenset({"nsfw_metatube_token"})


def organize_rules_snapshot(rules: OrganizeRules) -> dict[str, object]:
    """生成可持久化/返回前端的规则快照，不包含服务端密钥。"""
    payload = asdict(enforce_fixed_organize_rules(rules))
    for field_name in _ORGANIZE_RULE_SERVER_ONLY_FIELDS:
        payload.pop(field_name, None)
    return payload


def restore_organize_rules_snapshot(
    snapshot: object, *, trusted_rules: OrganizeRules | None = None,
) -> OrganizeRules:
    """从非敏感快照恢复规则；服务端字段始终取当前可信配置。"""
    if not isinstance(snapshot, dict):
        raise ValueError("整理规则快照无效")
    trusted = enforce_fixed_organize_rules(
        trusted_rules if trusted_rules is not None else OrganizeRules.from_config()
    )
    values = asdict(trusted)
    allowed_fields = set(OrganizeRules.__dataclass_fields__) - _ORGANIZE_RULE_SERVER_ONLY_FIELDS
    for key in allowed_fields:
        if key in snapshot:
            values[key] = snapshot[key]
    return enforce_fixed_organize_rules(OrganizeRules(**values))


def organize_rules_snapshot_matches(snapshot: object, current_rules: OrganizeRules) -> bool:
    """比较可执行规则；密钥轮换不复用历史值，而是使用当前服务端配置。"""
    if not isinstance(snapshot, dict):
        return False
    normalized = {
        key: value for key, value in snapshot.items()
        if key not in _ORGANIZE_RULE_SERVER_ONLY_FIELDS
    }
    return normalized == organize_rules_snapshot(current_rules)


class _LeasedNsfwRecognizerProxy:
    """兼容旧调用面，并把每次识别器方法调用纳入 lease 生命周期。"""

    def __init__(self, organizer: "Organizer", rules: OrganizeRules) -> None:
        self._organizer = organizer
        self._rules = copy.deepcopy(rules)

    def __getattr__(self, name: str):
        def invoke(*args, **kwargs):
            with self._organizer._nsfw_recognizer_lease(self._rules) as recognizer:
                if recognizer is None:
                    raise RuntimeError("MetaTube 识别器不可用或正在关闭")
                return getattr(recognizer, name)(*args, **kwargs)

        return invoke


class Organizer:
    def __init__(
        self, client: GuangYaClient = None, scraper: TMDBScraper = None, *,
        traversal_limits: tuple[int, int, int] | None = None,
    ):
        self._owns_client = client is None
        self._owns_scraper = scraper is None
        self.client = client if client is not None else GuangYaClient()
        self.scraper = scraper if scraper is not None else TMDBScraper()
        self._closed = False
        self._closing = False
        self._owned_scraper_closed = not self._owns_scraper
        self._owned_client_closed = not self._owns_client
        self._traversal_limits = traversal_limits or (
            max(8, min(get_int("GY_ORGANIZE_MAX_SCAN_DEPTH", DEFAULT_TRAVERSAL_MAX_DEPTH), 512)),
            max(100, min(get_int("GY_ORGANIZE_MAX_SCAN_DIRS", DEFAULT_TRAVERSAL_MAX_DIRS), 500_000)),
            max(1_000, min(get_int("GY_ORGANIZE_MAX_SCAN_ENTRIES", DEFAULT_TRAVERSAL_MAX_ENTRIES), 2_000_000)),
        )
        self._detail_cache: dict = {}
        self._forced_detail_refreshes: set[tuple[str, str]] = set()
        self._existing_variant_cache: dict[tuple[str, str, int, str], MediaVariant] = {}
        self._media_probe_payload_cache: dict[tuple[str, str, int], str] = {}
        self._media_probe_cache_checked: set[tuple[str, str, int]] = set()
        self._probe_budget = None
        self._nsfw_recognizers: dict[tuple[str, str, str, int], object] = {}
        self._close_call_lock = threading.Lock()
        self._nsfw_lock = threading.RLock()
        self._nsfw_active_leases: dict[int, int] = {}
        self._retired_nsfw_recognizers: dict[int, object] = {}

    @staticmethod
    def _close_nsfw_recognizer(recognizer: object) -> bool:
        close = getattr(recognizer, "close", None)
        if not callable(close):
            return True
        try:
            closed = close()
        except Exception as exc:
            logger.warning(
                "关闭 MetaTube 识别器失败 type=%s", type(exc).__name__
            )
            return False
        if closed is False:
            logger.warning("关闭 MetaTube 识别器未完成，已保留供后续重试")
            return False
        return True

    def _retire_nsfw_recognizer_locked(self, recognizer: object | None) -> list[object]:
        if recognizer is None:
            return []
        identity = id(recognizer)
        if self._nsfw_active_leases.get(identity, 0) > 0:
            self._retired_nsfw_recognizers[identity] = recognizer
            return []
        self._retired_nsfw_recognizers.pop(identity, None)
        return [recognizer]

    def _requeue_failed_nsfw_closes(self, recognizers: list[object]) -> None:
        if not recognizers:
            return
        with self._nsfw_lock:
            for recognizer in recognizers:
                self._retired_nsfw_recognizers[id(recognizer)] = recognizer

    def close(self) -> bool:
        """释放 Organizer 内部创建的长连接，注入依赖仍由调用方管理。"""
        with self._close_call_lock:
            with self._nsfw_lock:
                if self._closed:
                    return True
                self._closing = True
                recognizers_to_close: list[object] = []
                for recognizer in self._nsfw_recognizers.values():
                    recognizers_to_close.extend(
                        self._retire_nsfw_recognizer_locked(recognizer)
                    )
                self._nsfw_recognizers.clear()
                for identity, recognizer in list(
                    self._retired_nsfw_recognizers.items()
                ):
                    if self._nsfw_active_leases.get(identity, 0) <= 0:
                        self._retired_nsfw_recognizers.pop(identity, None)
                        recognizers_to_close.append(recognizer)

            seen: set[int] = set()
            failed_recognizers = []
            for recognizer in recognizers_to_close:
                identity = id(recognizer)
                if identity in seen:
                    continue
                seen.add(identity)
                if not self._close_nsfw_recognizer(recognizer):
                    failed_recognizers.append(recognizer)
            self._requeue_failed_nsfw_closes(failed_recognizers)

            with self._nsfw_lock:
                recognizers_drained = (
                    not self._nsfw_active_leases
                    and not self._retired_nsfw_recognizers
                )
            if not recognizers_drained:
                return False

            if self._owns_scraper and not self._owned_scraper_closed:
                close = getattr(self.scraper, "close", None)
                if callable(close):
                    try:
                        closed = close()
                    except Exception as exc:
                        logger.warning(
                            "关闭 TMDB Scraper 失败 type=%s",
                            type(exc).__name__,
                        )
                        closed = False
                    self._owned_scraper_closed = closed is not False
                else:
                    self._owned_scraper_closed = True
            if self._owns_client and not self._owned_client_closed:
                self._owned_client_closed = close_guangya_client(self.client)

            resources_closed = (
                self._owned_scraper_closed and self._owned_client_closed
            )
            if resources_closed:
                with self._nsfw_lock:
                    self._closed = True
                    self._closing = False
            return resources_closed

    @staticmethod
    def _parse_exts(raw: str, defaults: set[str]) -> set[str]:
        if not (raw or "").strip():
            return set(defaults)
        tokens = re.split(r"[,，\s]+", str(raw))
        parsed = {
            item.strip().lower().lstrip(".")
            for item in tokens
            if re.fullmatch(r"[A-Za-z0-9]{1,10}", item.strip().lstrip("."))
        }
        return parsed or set(defaults)

    def video_exts(self, rules: OrganizeRules) -> set[str]:
        return self._parse_exts(rules.video_exts, VIDEO_EXTS)

    def metadata_exts(self, rules: OrganizeRules) -> set[str]:
        return self._parse_exts(rules.metadata_exts, METADATA_EXTS)

    # ===== 分类与媒体身份 =====
    @staticmethod
    def _match_provider(match: MatchResult | None) -> str:
        if match is None:
            return ""
        provider = str(getattr(match, "provider", "") or "").strip().lower()
        if provider:
            return provider
        return "tmdb" if str(getattr(match, "tmdb_id", "") or "").strip() else ""

    @classmethod
    def _match_external_id(cls, match: MatchResult | None) -> str:
        if match is None:
            return ""
        external = str(getattr(match, "external_id", "") or "").strip()
        if external:
            return external
        return str(getattr(match, "tmdb_id", "") or "").strip()

    @classmethod
    def _match_identity_key(cls, match: MatchResult | None) -> str:
        provider = cls._match_provider(match)
        external = cls._match_external_id(match)
        return f"{provider}:{external}" if provider and external else ""

    @classmethod
    def _match_identity_tag(cls, match: MatchResult | None) -> str:
        provider = cls._match_provider(match)
        external = cls._match_external_id(match)
        if not provider or not external:
            return ""
        if provider == "tmdb":
            return f"{{tmdb-{external}}}"
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", external).strip("-._")
        provider_safe = re.sub(r"[^a-z0-9_-]+", "-", provider).strip("-_")
        return f"{{{provider_safe}-{safe}}}" if safe and provider_safe else ""

    def _detail(
        self, tmdb_id: str, media_type: str, *, force_refresh: bool = False
    ) -> dict:
        key = (tmdb_id, media_type)
        if force_refresh:
            self._detail_cache.pop(key, None)
        if key not in self._detail_cache:
            try:
                self._detail_cache[key] = self.scraper.get_detail(
                    tmdb_id, media_type, force_refresh=force_refresh
                )
            except TypeError:
                # 兼容测试桩与第三方 scraper；生产 TMDBScraper 支持受控刷新。
                self._detail_cache[key] = self.scraper.get_detail(tmdb_id, media_type)
        return self._detail_cache[key]

    def _detail_for_match(
        self, match: MatchResult, *, force_refresh: bool = False
    ) -> dict:
        metadata = getattr(match, "metadata", None)
        if self._match_provider(match) != "tmdb" and isinstance(metadata, dict):
            return dict(metadata)
        return (
            self._detail(match.tmdb_id, match.media_type, force_refresh=force_refresh)
            if match.tmdb_id else {}
        )

    def _refresh_tmdb_detail_once(
        self, match: MatchResult
    ) -> tuple[dict, bool]:
        """同一任务内每个 TMDB 身份最多绕过缓存复核一次。"""
        if self._match_provider(match) != "tmdb":
            return self._detail_for_match(match), False
        tmdb_id = str(getattr(match, "tmdb_id", "") or "").strip()
        media_type = str(getattr(match, "media_type", "") or "").strip().lower()
        if not tmdb_id or media_type not in {"tv", "movie"}:
            return self._detail_for_match(match), False
        key = (tmdb_id, media_type)
        if key in self._forced_detail_refreshes:
            return self._detail_for_match(match), False
        self._forced_detail_refreshes.add(key)
        return self._detail_for_match(match, force_refresh=True), True

    def _retry_inactive_retired_nsfw_locked(self) -> bool:
        """固定为 current + 至多一个 retired；active 旧代也会阻止扩代。"""
        blocked = False
        for identity, recognizer in list(self._retired_nsfw_recognizers.items()):
            if self._nsfw_active_leases.get(identity, 0) > 0:
                blocked = True
                continue
            if self._close_nsfw_recognizer(recognizer):
                self._retired_nsfw_recognizers.pop(identity, None)
            else:
                blocked = True
        return not blocked

    def _resolve_nsfw_recognizer_locked(
        self, rules: OrganizeRules,
    ) -> tuple[object | None, list[object]]:
        if self._closed or self._closing:
            return None, []
        if not rules.nsfw_enabled or not str(rules.nsfw_metatube_endpoint or "").strip():
            return None, []
        key = (
            str(rules.nsfw_metatube_endpoint).strip(),
            str(rules.nsfw_metatube_token or ""),
            str(rules.nsfw_strip_domains or ""),
            int(rules.nsfw_timeout_seconds),
        )
        if key in self._nsfw_recognizers:
            return self._nsfw_recognizers[key], []
        if not self._retry_inactive_retired_nsfw_locked():
            return None, []

        previous_close_failed = False
        for previous in self._nsfw_recognizers.values():
            identity = id(previous)
            if self._nsfw_active_leases.get(identity, 0) > 0:
                self._retired_nsfw_recognizers[identity] = previous
            elif not self._close_nsfw_recognizer(previous):
                self._retired_nsfw_recognizers[identity] = previous
                previous_close_failed = True
        self._nsfw_recognizers.clear()
        if previous_close_failed:
            return None, []

        from app.modules.nsfw import NsfwRecognizer
        try:
            recognizer = NsfwRecognizer(
                key[0], key[1], strip_domains=key[2], timeout=key[3]
            )
        except ValueError as exc:
            log_throttled(
                logger, logging.WARNING, f"metatube-config:{exc}",
                "MetaTube 配置无效，已跳过成人内容识别: %s", exc,
            )
            recognizer = None

        self._nsfw_recognizers[key] = recognizer
        return recognizer, []

    def _nsfw_recognizer(self, rules: OrganizeRules):
        """兼容纠错链路：实际方法调用仍通过 lease 保护底层识别器。"""
        with self._nsfw_lock:
            if (
                self._closed
                or self._closing
                or not rules.nsfw_enabled
                or not str(rules.nsfw_metatube_endpoint or "").strip()
            ):
                return None
        return _LeasedNsfwRecognizerProxy(self, rules)

    @contextmanager
    def _nsfw_recognizer_lease(self, rules: OrganizeRules):
        with self._nsfw_lock:
            recognizer, recognizers_to_close = self._resolve_nsfw_recognizer_locked(
                rules
            )
            if recognizer is not None:
                identity = id(recognizer)
                self._nsfw_active_leases[identity] = (
                    self._nsfw_active_leases.get(identity, 0) + 1
                )

        failed = [
            item
            for item in recognizers_to_close
            if not self._close_nsfw_recognizer(item)
        ]
        self._requeue_failed_nsfw_closes(failed)
        try:
            yield recognizer
        finally:
            if recognizer is not None:
                identity = id(recognizer)
                with self._nsfw_lock:
                    remaining = self._nsfw_active_leases.get(identity, 0) - 1
                    if remaining > 0:
                        self._nsfw_active_leases[identity] = remaining
                    else:
                        self._nsfw_active_leases.pop(identity, None)
                        retired = self._retired_nsfw_recognizers.get(identity)
                        if retired is not None and self._close_nsfw_recognizer(retired):
                            self._retired_nsfw_recognizers.pop(identity, None)

    @staticmethod
    def _nsfw_unresolved_match() -> MatchResult:
        """成人专用来源无法精确命中时保持待确认，绝不回退普通影视链。"""
        return MatchResult(
            media_type="movie",
            need_confirm=True,
            error="成人专用来源未提取到有效番号，或 MetaTube 没有返回完全一致的结果",
            status="no_match",
            matched_by="metatube_only",
            threshold=1.0,
            provider="metatube",
        )

    def classify(self, match: MatchResult,
                 rules: OrganizeRules | None = None) -> tuple[str, str, str]:
        """返回 (主类, 地区, 年份)，可选细分类严格受规则开关控制。"""
        rules = rules or OrganizeRules()
        detail = self._detail_for_match(match)
        if self._match_provider(match) in {"metatube", "clean_title"}:
            from app.modules.nsfw import validate_category_name
            return validate_category_name(rules.nsfw_category_name), "其他", (
                match.year or str(detail.get("release_date") or "")[:4]
            )
        genres = {g.get("id") for g in detail.get("genres", []) if isinstance(g, dict)}
        origin = detail.get("origin_country") or []
        country = str(origin[0]).upper() if origin else ""

        title_blob = " ".join(filter(None, [
            match.title,
            str(detail.get("name") or detail.get("title") or ""),
            str(detail.get("original_name") or detail.get("original_title") or ""),
        ]))
        concert_match = GENRE_MUSIC in genres and bool(CONCERT_RE.search(title_blob))
        kids_match = GENRE_KIDS in genres or (
            GENRE_FAMILY in genres and bool(KIDS_RE.search(title_blob))
        )
        if rules.add_concert and concert_match:
            main = "演唱会"
        elif rules.add_kids and kids_match:
            main = "儿童节目"
        elif match.media_type == "movie":
            # 动画电影仍属于电影；“动漫”专用于具有季集结构的动画剧集。
            # 这样输出目录的分类语义与再次识别时的 movie/tv 语义保持一致。
            main = "电影"
            if GENRE_DOC in genres:
                main = "纪录片"
        else:
            main = "剧集"
            if GENRE_ANIME in genres:
                main = "动漫"
            elif GENRE_DOC in genres:
                main = "纪录片"
            elif GENRE_VARIETY_REALITY in genres or GENRE_VARIETY_TALK in genres:
                main = "综艺"

        region = REGION_MAP.get(country, "其他")
        year = match.year or (
            (detail.get("first_air_date", "") or detail.get("release_date", ""))[:4]
        )
        return main, region, year

    # ===== 命名 =====
    def build_new_name(self, match: MatchResult, file: GuangYaFile,
                       parsed: dict, rules: OrganizeRules | None = None,
                       media_info_override: str = "", *,
                       media_variant_override: object | None = None,
                       include_media_info: bool = True,
                       include_variant_tags: bool = True) -> str:
        rules = enforce_fixed_organize_rules(rules or OrganizeRules())
        title = SAFE_RE.sub("_", match.title or "")
        year = match.year or ""
        tmdb = match.tmdb_id or ""
        identity_id = self._match_identity_key(match)
        identity_tag = self._match_identity_tag(match)
        ext = file.name.rsplit(".", 1)[-1] if "." in file.name else "mkv"
        is_tv = match.media_type == "tv" and parsed.get("season") is not None
        media_info = ""
        if include_media_info:
            media_info = media_info_override or (
                self._extract_media_info(file.name) if rules.media_info_enabled else ""
            )
        context = build_context(
            title=title,
            year=year,
            tmdb_id=tmdb,
            identity_id=identity_id,
            identity_tag=identity_tag,
            season=parsed.get("season"),
            episode=parsed.get("episode"),
            media_info=media_info,
            ext=ext,
            original_name=file.name,
        )
        scope = {part.strip() for part in str(rules.naming_scope or "guangya").split(",") if part.strip()}
        templates_enabled = "both" in scope or "guangya" in scope
        configured_template = rules.tv_template if is_tv else rules.movie_template

        def with_part_marker(rendered: str) -> str:
            if self._match_provider(match) not in {"metatube", "clean_title"}:
                return rendered
            part = parsed.get("part")
            try:
                part_index = int(part) if part is not None else None
            except (TypeError, ValueError):
                part_index = None
            if part_index is None or not (1 <= part_index <= 99):
                return rendered
            from app.modules.nsfw import extract_nsfw_part_index
            if extract_nsfw_part_index(rendered) == part_index:
                return rendered
            stem, separator, suffix = rendered.rpartition(".")
            # 年份为空的自定义模板可能在扩展名前留下连续点号；插入 CD 标记时
            # 顺手收敛尾部分隔符，避免生成 ``番号..CD1.mp4``。
            stem = stem.rstrip(" ._-")
            return (
                f"{stem}.CD{part_index}.{suffix}"
                if separator and stem and suffix
                else f"{rendered}.CD{part_index}"
            )

        def with_variant_tags(rendered: str) -> str:
            rendered = with_part_marker(rendered)
            if not include_variant_tags:
                return rendered
            variant = classify_variant(
                file.name,
                media_variant_override if media_variant_override is not None else media_info_override,
            )
            return append_variant_tags(rendered, variant.filename_tags(rules))

        if not templates_enabled or not configured_template:
            if is_tv:
                season = f"S{int(parsed['season']):02d}"
                ep = f"E{int(parsed['episode']):02d}" if parsed.get("episode") else ""
                suffix = f"-{media_info}" if media_info else ""
                rendered = f"{title}.{year}.{season}{ep}{suffix}.{ext}"
            else:
                marker = identity_tag or "{unidentified}"
                rendered = f"{title} ({year}){marker}.{ext}" if year else f"{title}{marker}.{ext}"
            return with_variant_tags(rendered)
        template = configured_template
        try:
            return with_variant_tags(render_template(template, context))
        except ValueError as exc:
            log_throttled(
                logger, logging.WARNING, f"file-template:{exc}",
                "命名模板无效，回退旧规则: %s", exc,
            )
            if is_tv:
                season = f"S{int(parsed['season']):02d}"
                ep = f"E{int(parsed['episode']):02d}" if parsed.get("episode") else ""
                suffix = f"-{media_info}" if media_info else ""
                rendered = f"{title}.{year}.{season}{ep}{suffix}.{ext}"
            else:
                marker = identity_tag or "{unidentified}"
                rendered = f"{title} ({year}){marker}.{ext}" if year else f"{title}{marker}.{ext}"
            return with_variant_tags(rendered)

    def build_show_dir(self, match: MatchResult, rules: OrganizeRules | None = None) -> str:
        """剧集/节目归档目录名。"""
        rules = enforce_fixed_organize_rules(rules or OrganizeRules())
        title = SAFE_RE.sub("_", match.title or "")
        year = match.year or ""
        tmdb = match.tmdb_id or ""
        identity_id = self._match_identity_key(match)
        identity_tag = self._match_identity_tag(match)
        context = build_context(
            title=title, year=year, tmdb_id=tmdb,
            identity_id=identity_id, identity_tag=identity_tag,
        )
        scope = {part.strip() for part in str(rules.naming_scope or "guangya").split(",") if part.strip()}
        fallback = f"{title} ({year}) {identity_tag}" if year else f"{title} {identity_tag}"
        if not ({"both", "guangya"} & scope) or not rules.show_dir_template:
            return fallback.strip()
        try:
            return render_template(rules.show_dir_template, context)
        except ValueError as exc:
            log_throttled(
                logger, logging.WARNING, f"show-template:{exc}",
                "剧集目录模板无效，回退旧规则: %s", exc,
            )
            return fallback.strip()

    def build_media_dir(self, match: MatchResult, rules: OrganizeRules | None = None) -> str:
        """电影和剧集共用的独立媒体目录入口。"""
        rules = enforce_fixed_organize_rules(rules or OrganizeRules())
        if match.media_type == "tv":
            return self.build_show_dir(match, rules)
        title = SAFE_RE.sub("_", match.title or "")
        year = match.year or ""
        tmdb = match.tmdb_id or ""
        identity_id = self._match_identity_key(match)
        identity_tag = self._match_identity_tag(match)
        context = build_context(
            title=title, year=year, tmdb_id=tmdb,
            identity_id=identity_id, identity_tag=identity_tag,
        )
        scope = {part.strip() for part in str(rules.naming_scope or "guangya").split(",") if part.strip()}
        fallback = f"{title} ({year}) {identity_tag}" if year else f"{title} {identity_tag}"
        if not ({"both", "guangya"} & scope) or not rules.movie_dir_template:
            return fallback.strip()
        try:
            return render_template(rules.movie_dir_template, context)
        except ValueError as exc:
            log_throttled(
                logger, logging.WARNING, f"movie-template:{exc}",
                "电影目录模板无效，回退默认规则: %s", exc,
            )
            return fallback.strip()

    @staticmethod
    def build_season_dir(match: MatchResult, parsed: dict | None = None) -> str:
        """返回剧集季目录；电影或无法确定季号时不额外分层。"""
        if match.media_type != "tv":
            return ""
        position = parsed or {}
        season = position.get("season")
        if season in (None, "") and position.get("episode") not in (None, ""):
            season = 1
        if isinstance(season, bool) or season in (None, ""):
            return ""
        try:
            season_number = int(season)
        except (TypeError, ValueError):
            return ""
        if season_number < 0:
            return ""
        if season_number == 0:
            return "Specials"
        return f"Season {season_number}"

    def build_media_path_parts(
        self, match: MatchResult, parsed: dict | None = None,
        rules: OrganizeRules | None = None,
    ) -> list[str]:
        """统一生成媒体自身目录层级：电影目录，或剧集目录/Season N。"""
        parts = [self.build_media_dir(match, rules)]
        season_dir = self.build_season_dir(match, parsed)
        if season_dir:
            parts.append(season_dir)
        return parts

    @staticmethod
    def _extract_media_info(filename: str) -> str:
        # 与在线探测使用同一套结构化回退，确保 ffprobe 失败时仍保留
        # WEB-DL/BluRay、分辨率、编码、音轨等文件名强证据。
        from app.modules.media_probe import infer_media_profile

        return infer_media_profile(filename).render()

    # ===== 覆盖策略 =====
    def should_replace(self, existing: GuangYaFile, new_file: GuangYaFile,
                       new_name: str, rules: OrganizeRules, *,
                       existing_evidence: str = "",
                       incoming_evidence: str = "") -> bool:
        """同集/同片冲突时，是否用新文件替换已有。"""
        if rules.conflict_strategy == 1:
            return False  # 不覆盖
        ex_score = self._priority_score(existing_evidence or existing.name, rules)
        new_score = self._priority_score(
            incoming_evidence or f"{new_file.name} {new_name}", rules
        )
        if rules.conflict_strategy == 2:  # 大文件优先
            if new_score > ex_score:
                return True
            if new_score == ex_score:
                if new_file.size != existing.size:
                    return new_file.size > existing.size
                return str(new_file.file_id) < str(existing.file_id)
            return False
        if rules.conflict_strategy == 3:  # 小文件优先
            if new_score > ex_score:
                return True
            if new_score == ex_score:
                if new_file.size != existing.size:
                    return new_file.size < existing.size
                return str(new_file.file_id) < str(existing.file_id)
            return False
        return False

    def _priority_score(self, name: str, rules: OrganizeRules) -> int:
        raw = str(name or "").lower()
        media_info = self._extract_media_info(name).lower()
        variant = classify_variant(name)
        score = 0
        if rules.remux_first and "remux" in raw:
            score += 100
        elif rules.remux_first and re.search(r"blu[ ._-]?ray", raw):
            score += 80
        if rules.resolution_first:
            if "2160p" in media_info or "2160p" in raw or "4k" in raw or "uhd" in raw:
                score += 40
            elif "1080p" in media_info or "1080p" in raw:
                score += 20
        if rules.dolby_first and (variant.dolby_vision is True or variant.atmos is True):
            score += 30
        return score

    def _validate_target_outside_source(self, source_id: str, target_id: str) -> None:
        """拒绝把归档目标放在来源目录自身或其子目录内。

        Web、TG、定时任务和下载联动在提交及执行前复用该校验，
        避免只依赖前端表单。
        """
        source_id = str(source_id or "0")
        current_id = str(target_id or "0")
        visited: set[str] = set()
        for _ in range(64):
            if current_id == source_id:
                raise DirectoryScrapeStateError("归档目标位于来源目录内，不能执行整理")
            if not current_id or current_id == "0" or current_id in visited:
                return
            visited.add(current_id)
            try:
                current = self.client.file_info(current_id)
            except Exception as exc:
                raise DirectoryScrapeStateError("无法校验光鸭整理归档目标") from exc
            if current is None:
                raise DirectoryScrapeStateError("光鸭整理归档目标不存在")
            if not current.is_dir:
                raise DirectoryScrapeStateError("光鸭整理归档目标不是目录")
            current_id = str(current.parent_id or "0")
        raise DirectoryScrapeStateError("归档目标目录层级异常")

    def _read_performance_snapshot(self) -> dict[str, int]:
        snapshot_reader = getattr(self.scraper, "performance_snapshot", None)
        if not callable(snapshot_reader):
            return {}
        snapshot = snapshot_reader()
        if not isinstance(snapshot, dict):
            return {}
        normalized: dict[str, int] = {}
        for key, value in snapshot.items():
            try:
                normalized[str(key)] = int(value)
            except (TypeError, ValueError, OverflowError):
                continue
        return normalized

    @staticmethod
    def _episode_evidence_group_key(
        item: _ScannedVideo,
        parent_context: str,
    ) -> str:
        """按目录与标题身份隔离连续集证据，避免同目录多作品互相污染。"""
        context = extract_recognition_context(item.file.name, parent_context)
        title = context.normalized_title or context.folder_title or context.filename_title
        identity = _normalize_media_identity(title)
        directory = item.relative_dir or "__root__"
        return f"{directory}\x1f{identity or '__unknown__'}"

    @staticmethod
    def _initial_stats() -> dict:
        return {
            "total": 0, "matched": 0, "need_confirm": 0,
            "confirmations": [], "skip_reasons": [],
            "directory_identity_cache_hits": 0,
            "directory_identity_cache_groups": 0,
            "recognition_work_cache_hits": 0,
            "recognition_work_cache_groups": 0,
            "directory_package_identity_bindings": 0,
            "directory_identity_attestation_bindings": 0,
            "directory_identity_attestation_hits": 0,
            "directory_special_identity_bindings": 0,
            "moved": 0, "renamed": 0, "rename_failed": 0,
            "metadata_moved": 0, "stopped": 0, "skipped": 0,
            "conflict": 0, "failed": 0,
            "subtitle_moved": 0, "subtitle_skipped": 0,
            "subtitle_reasons": [], "scan_errors": [],
            "scan_complete": True, "scan_limited": 0,
            "scan_limit_kind": "", "scan_dirs": 0,
            "scan_entries": 0, "scan_duplicate_dirs": 0,
            "scan_list_dir_calls": 0, "scan_file_info_calls": 0,
            "source_groups_total": 0, "source_groups_completed": 0,
            "current_source_group": "", "source_groups": [],
            "strm_changes": [], "strm_force_full": False,
            "specials_auto_mapped": 0,
            "fractional_specials_mapped": 0,
            "empty_dir_cleanup_reasons": [],
        }

    def _scan_source(
        self, context: OrganizeContext, rules: OrganizeRules, stats: dict,
        *, restriction: ScanRestriction | None = None,
    ) -> OrganizeScanResult:
        """只读扫描来源目录，并返回后续阶段所需的完整快照。"""
        scanner = OrganizerScanner(
            self.client,
            traversal_limits=self._traversal_limits,
            append_reason=self._append_reason,
        )
        return scanner.scan(
            context,
            rules,
            stats,
            video_exts=self.video_exts(rules),
            metadata_exts=self.metadata_exts(rules),
            restriction=restriction,
        )

    def _build_plans(
        self, scan_result: OrganizeScanResult, context: OrganizeContext,
        rules: OrganizeRules, stats: dict, performance_before: dict[str, int],
        *,
        media_profile_loader: Callable[[GuangYaFile, OrganizePlan], object | None] | None = None,
        target_inventory_loader: Callable[
            [OrganizePlan], tuple[str | None, list[GuangYaFile], dict[str, str]]
        ] | None = None,
        identity_history_loader: Callable[
            [OrganizePlan], set[tuple[str, str]]
        ] | None = None,
    ) -> OrganizePlanningResult:
        """识别媒体、生成命名计划、规划伴随文件并完成只读冲突预演。"""
        scanned_videos = scan_result.scanned_videos
        companion_files = scan_result.companion_files
        video_files_by_path = scan_result.video_files_by_path
        source_root_name = scan_result.source_root_name
        source_dir_id = context.source_dir_id
        probe_cache_only = context.probe_cache_only
        automatic = context.automatic
        plans: list[OrganizePlan] = []
        source_files_by_id = {
            str(item.file.file_id): item.file for item in scanned_videos
        }
        source_probe_cache: dict[tuple[str, str, int], str] = {}
        source_probe_cache_loaded = False
        stats["media_probe_cache_batches"] = 0
        stats["media_probe_cache_hits"] = 0
        if (
            rules.media_info_enabled
            and rules.media_probe_enabled
            and scanned_videos
        ):
            probe_versions = [
                (
                    str(item.file.file_id),
                    str(item.file.etag or ""),
                    int(item.file.size or 0),
                )
                for item in scanned_videos
            ]
            try:
                source_probe_cache = db.get_media_probe_cache_many(
                    probe_versions, allow_fingerprint_fallback=True
                )
                source_probe_cache_loaded = True
                self._media_probe_cache_checked.update(probe_versions)
                self._media_probe_payload_cache.update(source_probe_cache)
                stats["media_probe_cache_batches"] = 1
                stats["media_probe_cache_hits"] = len(source_probe_cache)
            except Exception as exc:
                logger.warning(
                    "批量读取媒体探测缓存失败，回退逐文件读取 type=%s",
                    type(exc).__name__,
                )
        recognition_started = time.monotonic()
        special_positions = self._special_position_overrides(scanned_videos)
        stats["specials_auto_mapped"] = len(special_positions)
        stats["fractional_specials_mapped"] = sum(
            1
            for item in scanned_videos
            if item.file.file_id in special_positions
            and has_fractional_episode_position(item.file.name)
        )
        recognition_parent_paths: dict[str, str] = {}
        source_positions: dict[str, tuple[int | None, int | None]] = {}
        episode_evidence_keys: dict[str, str] = {}
        evidence_entries: list[tuple[str, str, int | None, int | None]] = []
        directory_video_member_counts: dict[str, int] = {}
        parse_source_position_impl = getattr(type(self.scraper), "parse_source_position", None)
        for item in scanned_videos:
            parent_context = "/".join(
                value for value in (source_root_name, item.recognition_parent_path) if value
            )
            recognition_parent_paths[item.file.file_id] = parent_context
            evidence_key = self._episode_evidence_group_key(item, parent_context)
            episode_evidence_keys[item.file.file_id] = evidence_key
            if not item.special:
                # 目录级低置信身份凭证必须覆盖同一物理目录中的全部普通
                # 视频。若混入重复版本、无法解析集号或另一部作品，物理
                # 成员数会大于连续剧集证据数，整包失败关闭，避免只放行
                # “看起来连续”的子集。
                physical_directory = item.relative_dir or "__root__"
                directory_video_member_counts[physical_directory] = (
                    directory_video_member_counts.get(physical_directory, 0) + 1
                )
            if callable(parse_source_position_impl):
                try:
                    position = self.scraper.parse_source_position(item.file.name, parent_context)
                except Exception:
                    position = (None, None)
                if isinstance(position, (tuple, list)) and len(position) == 2:
                    normalized_position = (position[0], position[1])
                    # 只有明确集号才构成可执行的位置覆盖。``(None, None)``
                    # 或 ``(S, None)`` 继续向下传都会绕过“剧集缺少集号”的
                    # 安全门，导致异常文件按电影式命名。
                    if normalized_position[1] is not None:
                        source_positions[item.file.file_id] = normalized_position
                        evidence_entries.append((
                            evidence_key,
                            parent_context,
                            position[0],
                            position[1],
                        ))
        directory_episode_evidence = build_directory_episode_evidence(evidence_entries)
        directory_identity_cache: dict[tuple[str, str, str, str], MatchResult] = {}
        directory_identity_hit_groups: set[tuple[str, str, str, str]] = set()
        recognition_work_cache: dict[tuple[str, str, str, bool, str], MatchResult] = {}
        recognition_work_hit_groups: set[tuple[str, str, str, bool, str]] = set()
        directory_identity_attestations: dict[
            tuple[str, str, str, str], dict[str, object]
        ] = {}
        recognition_names: dict[str, str] = {}

        # 目录身份识别名使用连续包的末集作为最强位置证据。为了让该证据
        # 在首个普通文件上完成严格 TMDB 校验并生成目录身份凭证，规划阶段
        # 先处理每组的末集代表文件，再处理同组其余成员。最终结果会恢复原始
        # 扫描顺序，因此这里只改变只读识别顺序，不改变执行顺序或日志顺序。
        episode_groups: dict[str, list[_ScannedVideo]] = {}
        for item in scanned_videos:
            evidence_key = episode_evidence_keys.get(item.file.file_id, "")
            if item.special or evidence_key not in directory_episode_evidence:
                continue
            episode_groups.setdefault(evidence_key, []).append(item)

        planning_videos: list[_ScannedVideo] = []
        emitted_episode_groups: set[str] = set()
        for item in scanned_videos:
            evidence_key = episode_evidence_keys.get(item.file.file_id, "")
            evidence = directory_episode_evidence.get(evidence_key)
            if item.special or evidence is None:
                planning_videos.append(item)
                continue
            if evidence_key in emitted_episode_groups:
                continue
            emitted_episode_groups.add(evidence_key)
            group = episode_groups.get(evidence_key, [item])
            representative: list[_ScannedVideo] = []
            remainder: list[_ScannedVideo] = []
            for candidate in group:
                candidate_position = source_positions.get(candidate.file.file_id)
                try:
                    is_endpoint = bool(
                        candidate_position is not None
                        and candidate_position[1] is not None
                        and int(candidate_position[1]) == int(evidence.range_end)
                    )
                except (TypeError, ValueError):
                    is_endpoint = False
                (representative if is_endpoint else remainder).append(candidate)
            planning_videos.extend(representative + remainder)

        planned_file_ids: list[str] = []
        for item in planning_videos:
            position = special_positions.get(item.file.file_id)
            recognition_name = ""
            explicit_file_marker = _has_explicit_tmdb_marker(item.file.name)
            configured_media_type_hint = str(
                context.media_type_hint or ""
            ).strip().lower()
            if configured_media_type_hint not in {"movie", "tv"}:
                configured_media_type_hint = ""
            recognition_media_type_hint = (
                "tv"
                if position is not None and explicit_file_marker
                else configured_media_type_hint
            )
            if position is not None and not explicit_file_marker:
                # S00/OVA/NCOP 文件若自身带有完整作品名，优先使用清洗后的
                # 文件标题，避免父目录中的发布组/打包标签压低匹配分数。
                # 裸 NCOP/OVA 等会失败关闭并回退所属作品目录。
                title_hint = _special_filename_identity_hint(item.file.name)
                if not title_hint:
                    title_hint = title_hint_from_path(
                        item.recognition_parent_path, source_root_name
                    )
                if title_hint:
                    ext = item.file.name.rsplit(".", 1)[-1] if "." in item.file.name else "mkv"
                    # 特别篇的 S00E## 是本地稳定排序号，不应参与 TMDB Season 0
                    # 硬校验；识别阶段使用所属作品的普通季号只确认 TV 身份，
                    # 归档阶段再通过 parsed_override 注入稳定的 S00E##。
                    identity_context = extract_recognition_context(
                        title_hint, item.recognition_parent_path
                    )
                    identity_season = (
                        identity_context.season
                        if identity_context.season not in (None, 0)
                        else 1
                    )
                    recognition_name = f"{title_hint}.S{identity_season:02d}.{ext}"
            parent_context = recognition_parent_paths.get(
                item.file.file_id, item.recognition_parent_path
            )
            episode_evidence = directory_episode_evidence.get(
                episode_evidence_keys.get(item.file.file_id, "")
            )
            if (
                recognition_name == ""
                and episode_evidence is not None
                and not explicit_file_marker
            ):
                # 连续剧集包已有“同标题 + 连续集号”的目录级证据时，优先
                # 使用单文件解析出的干净作品名，避免根目录中的发布组、打包
                # 标签成为严格标题锚点。文件名不足时仍回退原有路径标题。
                title_hint = _directory_episode_identity_hint(
                    item.file.name, parent_context
                )
                if not title_hint:
                    title_hint = title_hint_from_path(parent_context, source_root_name)
                if title_hint:
                    ext = item.file.name.rsplit(".", 1)[-1] if "." in item.file.name else "mkv"
                    # 目录级连续集包先识别作品身份，但不能把父目录已经明确的
                    # 季号强行降成 S01：这会让 ``Season 3`` / ``TV-3`` 按第一季
                    # 校验，丢失目标季年份证据并制造不必要的人工确认。识别位置
                    # 使用该组连续证据的末集，而不是固定 E01：末集可以严格排除
                    # “同名但总集数更短”的旧作，也能让发布方 S02E01-E33 与
                    # TMDB 合并季 S01E53-E85 的分割放送映射参与候选消歧。真实
                    # 单文件位置仍由 source_position_override 恢复并逐文件复核。
                    source_position = source_positions.get(item.file.file_id)
                    if source_position is not None and source_position[0] not in (None, 0):
                        identity_season = int(source_position[0])
                    else:
                        identity_season = 1
                    identity_year = _recognition_identity_year(
                        item.file.name, parent_context
                    )
                    year_token = f".{identity_year}" if identity_year else ""
                    identity_episode = max(
                        1,
                        int(episode_evidence.range_end),
                    )
                    recognition_name = (
                        f"{title_hint}{year_token}.S{identity_season:02d}"
                        f"E{identity_episode:02d}.{ext}"
                    )
            recognition_names[item.file.file_id] = recognition_name
            cache_key = self._directory_identity_cache_key(
                item, rules, parent_path_override=parent_context
            )
            cached_match = directory_identity_cache.get(cache_key) if cache_key else None
            directory_identity_attestation = (
                directory_identity_attestations.get(cache_key) if cache_key else None
            )
            if directory_identity_attestation is not None:
                stats["directory_identity_attestation_hits"] += 1
            if cached_match is not None:
                stats["directory_identity_cache_hits"] += 1
                directory_identity_hit_groups.add(cache_key)
            work_cache_key = self._recognition_work_cache_key(
                recognition_name=recognition_name,
                parent_path=parent_context,
                media_type_hint=recognition_media_type_hint,
                rules=rules,
                automatic=automatic,
                trusted_match_override=cached_match,
            )
            if work_cache_key is not None and work_cache_key in recognition_work_cache:
                stats["recognition_work_cache_hits"] += 1
                recognition_work_hit_groups.add(work_cache_key)
            plan = self._plan_one(
                item.file,
                item.relative_dir,
                rules,
                recognition_parent_path=parent_context,
                # 首次识别必须保留识别器已经通过 TMDB 详情验证的最终位置
                # （例如 One Piece E1173 -> S23E18），因此普通源位置不能无条件
                # 覆盖 parsed。目录身份缓存命中时，match 的识别名是为复用身份
                # 构造的目录身份识别名；此时必须从当前文件恢复原始季集，再由
                # 下方 episode mapping 针对当前文件重新换算。身份可以缓存，位置
                # 绝不能跨文件缓存。
                parsed_override=(
                    position
                    if position is not None
                    else (
                        source_positions.get(item.file.file_id)
                        if cached_match is not None
                        else None
                    )
                ),
                source_position_override=source_positions.get(item.file.file_id),
                directory_episode_evidence=episode_evidence,
                directory_episode_member_count=directory_video_member_counts.get(
                    item.relative_dir or "__root__", 0
                ),
                recognition_name=recognition_name,
                recognition_media_type_hint=recognition_media_type_hint,
                match_override=cached_match,
                directory_identity_attestation=directory_identity_attestation,
                recognition_work_cache=recognition_work_cache,
                recognition_work_cache_key=work_cache_key,
                # 识别阶段只读取预取缓存；在线 ffprobe 在全部身份安全门通过后
                # 统一有界并发执行，避免数十集逐文件串行阻塞。
                media_probe_cache_only=True,
                media_probe_cached_payload=source_probe_cache.get((
                    str(item.file.file_id),
                    str(item.file.etag or ""),
                    int(item.file.size or 0),
                ), ""),
                media_probe_cache_prefetched=source_probe_cache_loaded,
                automatic=automatic,
            )
            plans.append(plan)
            if cache_key and directory_identity_attestation is None:
                accepted_attestation = self._accepted_directory_identity_attestation(
                    plan.match
                )
                if accepted_attestation is not None:
                    directory_identity_attestations[cache_key] = accepted_attestation
                    stats["directory_identity_attestation_bindings"] += 1
            if cache_key and cached_match is None:
                directory_package_proof = (
                    self._accepted_directory_package_identity_proof(plan.match)
                )
                if directory_package_proof is not None:
                    directory_identity_cache[cache_key] = (
                        self._identity_only_directory_match(plan.match)
                    )
                    stats["directory_package_identity_bindings"] += 1
                elif self._cacheable_directory_match(
                    plan.match,
                    automatic_threshold=automatic_match_policy(
                        rules.automatic_match_preset
                    ).threshold,
                    automatic_preset=rules.automatic_match_preset,
                ):
                    directory_identity_cache[cache_key] = copy.deepcopy(plan.match)
            planned_file_ids.append(str(item.file.file_id))

        if planning_videos != scanned_videos:
            plans_by_file_id = {
                file_id: plan for file_id, plan in zip(planned_file_ids, plans)
            }
            plans = [
                plans_by_file_id[str(item.file.file_id)] for item in scanned_videos
            ]

        # 本次源扫描内的普通剧集已经通过严格自动校验时，可为“自身也独立
        # 命中同一 TMDB、仅因特殊内容标题残片而被拦截”的 OVA/NCOP/Extra
        # 复用作品身份。这里只复用身份，不复用季集位置；特殊内容仍由稳定
        # 的 S00E## 分配器独立编号。特殊文件自身没有命中相同 ID 时失败关闭。
        if automatic and isinstance(self.scraper, TMDBScraper) and not rules.nsfw_enabled:
            trusted_by_scope_and_tmdb: dict[tuple[str, str], MatchResult] = {}
            for donor_item, donor_plan in zip(scanned_videos, plans):
                if donor_item.special or donor_plan.action != "move":
                    continue
                donor_id = self._trusted_directory_tv_identity(
                    donor_plan.match,
                    threshold=automatic_match_policy(
                        rules.automatic_match_preset
                    ).threshold,
                    automatic_preset=rules.automatic_match_preset,
                )
                if not donor_id:
                    continue
                trusted_by_scope_and_tmdb.setdefault(
                    (
                        self._physical_source_root(donor_item),
                        donor_id,
                    ),
                    copy.deepcopy(donor_plan.match),
                )

            for index, (item, original_plan) in enumerate(zip(scanned_videos, plans)):
                if not item.special or original_plan.action != "skip":
                    continue
                if not is_special_media_name(item.file.name):
                    continue
                binding_parent_context = recognition_parent_paths.get(
                    item.file.file_id, item.recognition_parent_path
                )
                if _has_explicit_tmdb_marker(
                    f"{item.file.name} {binding_parent_context}"
                ):
                    continue
                special_tmdb_id = str(
                    getattr(original_plan.match, "tmdb_id", "") or ""
                ).strip()
                donor_match = trusted_by_scope_and_tmdb.get((
                    self._physical_source_root(item),
                    special_tmdb_id,
                ))
                if donor_match is None:
                    continue
                if not self._special_match_can_bind_identity(
                    original_plan.match, special_tmdb_id
                ):
                    continue
                identity_match = self._identity_only_directory_match(donor_match)
                parent_context = recognition_parent_paths.get(
                    item.file.file_id, item.recognition_parent_path
                )
                retried = self._plan_one(
                    item.file,
                    item.relative_dir,
                    rules,
                    recognition_parent_path=parent_context,
                    parsed_override=special_positions.get(item.file.file_id),
                    source_position_override=source_positions.get(item.file.file_id),
                    directory_episode_evidence=directory_episode_evidence.get(
                        episode_evidence_keys.get(item.file.file_id, "")
                    ),
                    recognition_name=recognition_names.get(item.file.file_id, ""),
                    match_override=identity_match,
                    media_probe_cache_only=True,
                    media_probe_cached_payload=source_probe_cache.get((
                        str(item.file.file_id),
                        str(item.file.etag or ""),
                        int(item.file.size or 0),
                    ), ""),
                    media_probe_cache_prefetched=source_probe_cache_loaded,
                    automatic=True,
                )
                if retried.action != "move" or retried.match is None:
                    continue
                metadata = dict(getattr(retried.match, "metadata", None) or {})
                metadata["directory_special_identity_binding"] = {
                    "tmdb_id": special_tmdb_id,
                    "source": "verified_regular_same_scan",
                }
                retried.match.metadata = metadata
                plans[index] = retried
                stats["directory_special_identity_bindings"] += 1

        # 目录分组仅用于进度与审计；所有组仍必须先完成全量扫描、识别和
        # 跨组冲突预演，之后才允许首个云盘写入。这样既能逐目录观察进度，
        # 又不会退化成“边扫边移动”而漏掉后续目录中的同版本冲突。
        source_group_rows: dict[str, dict[str, object]] = {}
        for item, plan in zip(scanned_videos, plans):
            plan.source_group_id = str(item.source_group_id or source_dir_id)
            plan.source_group_path = str(item.source_group_path or "__root__")
            group_key = f"{plan.source_group_id}\x1f{plan.source_group_path}"
            row = source_group_rows.setdefault(group_key, {
                "id": plan.source_group_id,
                "path": plan.source_group_path,
                "name": (
                    source_root_name
                    if plan.source_group_path == "__root__"
                    else plan.source_group_path
                ),
                "status": "planned",
                "total": 0,
                "moved": 0,
                "metadata_moved": 0,
                "skipped": 0,
                "need_confirm": 0,
                "failed": 0,
            })
            row["total"] = int(row["total"]) + 1
        stats["source_groups"] = list(source_group_rows.values())
        stats["source_groups_total"] = len(source_group_rows)

        stats["directory_identity_cache_groups"] = len(directory_identity_hit_groups)
        stats["recognition_work_cache_groups"] = len(recognition_work_hit_groups)
        stats["recognition_elapsed_seconds"] = round(
            time.monotonic() - recognition_started, 3
        )
        performance_after = self._read_performance_snapshot()
        for key, value in performance_after.items():
            stats[key] = max(0, value - performance_before.get(key, 0))
        if media_profile_loader is None:
            # 从真正进入在线探测时才启动墙钟预算，避免 TMDB/AI 识别耗时
            # 提前消耗探测窗口。每个扫描视频仍保留首次和一次瞬态重试额度。
            from app.modules.media_probe import ProbeBudget
            probe_budget_seconds = float(
                max(10, min(60, int(rules.media_probe_timeout or 30) * 2))
            )
            self._probe_budget = ProbeBudget(
                max(24, len(scanned_videos) * 2),
                max_seconds=probe_budget_seconds,
            )
            stats["media_probe_budget_seconds"] = probe_budget_seconds
            self._probe_move_plan_profiles(
                plans,
                source_files_by_id,
                source_probe_cache,
                cache_prefetched=source_probe_cache_loaded,
                rules=rules,
                automatic=automatic,
                cache_only=probe_cache_only,
                stats=stats,
                cancel_event=context.cancel_event,
            )
        else:
            # 规划策略不关心媒体来自云端还是本地。来源适配器只返回统一
            # MediaProfile，命名与版本身份仍由 Organizer 的同一函数重算。
            profile_started = time.monotonic()
            loaded_profiles = 0
            failed_profiles = 0
            for plan in plans:
                if plan.action != "move" or plan.match is None:
                    continue
                source_file = source_files_by_id.get(str(plan.file_id or ""))
                if source_file is None:
                    continue
                try:
                    profile = media_profile_loader(source_file, plan)
                except Exception as exc:
                    failed_profiles += 1
                    logger.warning(
                        "来源媒体探测失败 file_id=%s type=%s",
                        plan.file_id,
                        type(exc).__name__,
                    )
                    profile = None
                if profile is None:
                    plan.media_probe_pending = False
                    continue
                self._apply_media_profile_to_move_plan(
                    plan,
                    source_file,
                    rules,
                    plan.match,
                    {
                        "season": plan.season,
                        "episode": plan.episode,
                        "part": plan.multipart_index,
                    },
                    profile,
                )
                plan.media_probe_complete = True
                plan.media_probe_pending = False
                loaded_profiles += 1
            stats["media_probe_online_candidates"] = sum(
                1 for plan in plans if plan.action == "move" and plan.match is not None
            )
            stats["media_probe_online_profiles"] = loaded_profiles
            stats["media_probe_failures"] = failed_profiles
            stats["media_probe_elapsed_seconds"] = round(
                time.monotonic() - profile_started, 3
            )
        self._apply_media_source_consensus(
            plans, source_files_by_id, rules=rules, stats=stats,
        )
        subtitle_plans_by_video: dict[str, list] = {}
        for rel, candidates in companion_files.items():
            subtitles = [item for item in candidates if media_role(item.name) == "subtitle"]
            result = plan_subtitle_companions(video_files_by_path.get(rel, []), subtitles)
            for subtitle_plan in result.plans:
                subtitle_plans_by_video.setdefault(subtitle_plan.video_file_id, []).append(subtitle_plan)
            stats["subtitle_skipped"] += len(result.skipped)
            for skipped in result.skipped:
                if skipped.reason not in stats["subtitle_reasons"]:
                    stats["subtitle_reasons"].append(skipped.reason)
        self._apply_nsfw_multipart_policy(plans, rules)
        stats["confirmation_groups"] = self._build_confirmation_groups(
            plans,
            companion_files,
            source_dir_id=source_dir_id,
            source_name=source_root_name,
            rules=rules,
        )
        # 在首次云盘写入前完成身份保护与同批版本仲裁。执行阶段仍会实时
        # 复核目标目录，以覆盖并发外部修改，但批内败者不会再先移动后替换。
        conflict_started = time.monotonic()
        if target_inventory_loader is None:
            self._preview_conflicts(plans, rules)
        else:
            self._preview_conflicts_with_inventory(
                plans, rules, target_inventory_loader,
                identity_history_loader=identity_history_loader,
            )
        stats["conflict_check_elapsed_seconds"] = round(
            time.monotonic() - conflict_started, 3
        )
        # 统计识别层面（dry_run 与实际执行都统计）
        for p in plans:
            if p.action == "move" and p.match and p.match.tmdb_id:
                stats["matched"] += 1
            elif p.action == "conflict":
                stats["failed"] += 1
            elif p.action == "skip":
                if p.match and p.match.need_confirm:
                    stats["need_confirm"] += 1
                    summary = self._confirmation_summary(p.match)
                    if summary and summary not in stats["confirmations"] and len(stats["confirmations"]) < 3:
                        stats["confirmations"].append(summary)
                else:
                    stats["skipped"] += 1
                    self._append_reason(
                        stats, "skip_reasons",
                        p.note or p.conflict_note or (p.match.error if p.match else "")
                        or "未进入整理执行",
                    )
        logger.info("整理扫描完成 %s", _format_scan_summary(stats))
        return OrganizePlanningResult(
            plans=plans,
            subtitle_plans_by_video=subtitle_plans_by_video,
        )

    def plan_scan_result(
        self,
        scan_result: OrganizeScanResult,
        rules: OrganizeRules,
        *,
        automatic: bool = False,
        source_dir_id: str = "",
        source_name: str = "",
        media_type_hint: str = "",
        media_profile_loader: Callable[[GuangYaFile, OrganizePlan], object | None] | None = None,
        target_inventory_loader: Callable[
            [OrganizePlan], tuple[str | None, list[GuangYaFile], dict[str, str]]
        ] | None = None,
        identity_history_loader: Callable[
            [OrganizePlan], set[tuple[str, str]]
        ] | None = None,
    ) -> tuple[OrganizePlanningResult, dict]:
        """对已由来源适配器生成的稳定快照运行统一只读规划。

        光鸭和本地只需要分别提供扫描快照、媒体探测与目标库存读取；身份
        识别、目录证据、季集映射、特殊篇、命名和冲突策略均经过同一入口。
        本方法不会执行移动、重命名、删除、清理或整理后联动。
        """
        effective_rules = enforce_fixed_organize_rules(rules)
        self._reset_task_caches()
        stats = self._initial_stats()
        stats["total"] = len(scan_result.scanned_videos)
        context = OrganizeContext(
            source_dir_id=str(source_dir_id or "planning"),
            dry_run=True,
            post_actions=False,
            source_name=str(source_name or scan_result.source_root_name or ""),
            media_probe_cache_only=True,
            automatic=bool(automatic),
            group_pipeline=False,
            media_type_hint=str(media_type_hint or ""),
        )
        performance_before = self._read_performance_snapshot()
        result = self._build_plans(
            scan_result,
            context,
            effective_rules,
            stats,
            performance_before,
            media_profile_loader=media_profile_loader,
            target_inventory_loader=target_inventory_loader,
            identity_history_loader=identity_history_loader,
        )
        return result, stats

    @staticmethod
    def _validate_scan_for_execution(context: OrganizeContext, stats: dict) -> None:
        """写入任务必须基于完整目录快照；预览允许返回明确标记的部分结果。"""
        if context.dry_run:
            return
        if stats.get("scan_limited"):
            if stats.get("scan_limit_kind") == "max_files":
                raise OrganizeScanUnsafeError(
                    "目录扫描达到文件数量上限，未执行整理；请取消数量限制后重试"
                )
            raise OrganizeScanUnsafeError(
                "目录扫描超过安全上限，未执行整理；请缩小来源范围后重试"
            )
        if context.require_complete_scan and stats.get("scan_errors"):
            raise OrganizeScanUnsafeError(
                "目录扫描不完整，已在首次云盘写入前终止，请稍后重试"
            )

    def _run_execution_stage(
        self, scan_result: OrganizeScanResult, planning_result: OrganizePlanningResult,
        context: OrganizeContext, rules: OrganizeRules, stats: dict,
        *, on_progress: Callable[[int, int], None] | None = None,
        on_stage: Callable[[str], None] | None = None,
    ) -> None:
        """串行执行已经完成冲突预演的计划；该边界禁止并发写入。"""
        if context.dry_run:
            return
        self._validate_scan_for_execution(context, stats)
        plans = planning_result.plans
        subtitle_plans_by_video = planning_result.subtitle_plans_by_video
        companion_files = scan_result.companion_files
        scanned_dirs = scan_result.scanned_dirs
        protected_sources = scan_result.protected_sources
        source_dir_id = context.source_dir_id
        cancel_event = context.cancel_event
        post_actions = context.post_actions
        source_name = context.source_name
        if on_stage is not None:
            on_stage(GROUP_STAGE_EXECUTE)
        execute_started = time.monotonic()
        execute_organize_plans(
            self,
            plans, rules, stats, companion_files, subtitle_plans_by_video,
            cancel_event, source_dir_id=source_dir_id,
            on_progress=on_progress,
            operation_token=context.operation_token,
        )
        stats["execute_elapsed_seconds"] = round(
            time.monotonic() - execute_started, 3
        )
        cleanup_safe = not (
            context.cancelled()
            or stats.get("stopped")
            or stats.get("failed")
            or stats.get("scan_errors")
            or stats.get("replacement_cleanup_failed")
            or stats.get("audit_failures")
        )
        cleanup_started = time.monotonic()
        if rules.clean_empty and cleanup_safe:
            if on_stage is not None:
                on_stage(GROUP_STAGE_CLEANUP)
            cleanup_report = self._clean_empty_dirs_report(
                scanned_dirs,
                protected_source_ids=protected_sources,
            )
            stats["empty_dirs_cleaned"] = int(cleanup_report.get("cleaned", 0) or 0)
            for report_key, stats_key in (
                ("candidates", "empty_dir_cleanup_candidates"),
                ("protected", "empty_dir_cleanup_protected"),
                ("not_empty", "empty_dir_cleanup_not_empty"),
                ("unavailable", "empty_dir_cleanup_unavailable"),
            ):
                value = int(cleanup_report.get(report_key, 0) or 0)
                if value:
                    stats[stats_key] = value
            if cleanup_report.get("delete_failures"):
                stats["empty_dir_cleanup_failed"] = int(
                    cleanup_report.get("delete_failures", 0) or 0
                )
            if cleanup_report.get("unsupported"):
                stats["empty_dir_cleanup_unsupported"] = int(
                    cleanup_report.get("unsupported", 0) or 0
                )
            for reason in cleanup_report.get("reasons", []) or []:
                self._append_reason(
                    stats, "empty_dir_cleanup_reasons", reason, limit=6
                )
        elif rules.clean_empty and not cleanup_safe:
            stats["empty_dir_cleanup_skipped"] = 1
            self._append_reason(
                stats,
                "empty_dir_cleanup_reasons",
                "整理存在失败或扫描异常，已保留目录以便恢复",
                limit=6,
            )
            logger.warning("整理存在失败或扫描异常，已跳过空目录清理以保留恢复现场")
        stats["cleanup_elapsed_seconds"] = round(
            time.monotonic() - cleanup_started, 3
        )
        if post_actions and not context.cancelled():
            self._run_post_actions(stats, rules, source_name=source_name)

    def _run_post_actions(
        self, stats: dict, rules: OrganizeRules, *, source_name: str = "",
    ) -> None:
        """单来源整理结束后的 STRM 联动与结果通知。

        组级流水线在全部媒体组结束后调用一次，避免每组重复触发同步和通知。
        """
        unsafe_partial = bool(
            stats.get("failed")
            or stats.get("scan_errors")
            or stats.get("replacement_cleanup_failed")
        )
        if rules.link_strm and stats.get("moved", 0) and (
            not unsafe_partial or stats.get("strm_changes")
        ):
            self._post_organize_link(
                stats, rules, force_incremental=unsafe_partial,
            )
        elif rules.link_strm and unsafe_partial:
            stats["strm"] = {
                "ok": False,
                "skipped": True,
                "error": "整理存在失败项且没有已确认的变更清单，已停止 STRM 同步",
            }
        self._notify_result(stats, rules, source_name=source_name)

    # ===== 整理主流程 =====
    def organize(self, source_dir_id: str, rules: OrganizeRules,
                 dry_run: bool = True, max_files: int = 0,
                 cancel_event: threading.Event | None = None, *,
                 post_actions: bool = True,
                 source_name: str = "",
                 require_complete_scan: bool = False,
                 media_probe_cache_only: bool | None = None,
                 protected_source_ids: set[str] | None = None,
                 automatic: bool = False,
                 group_progress: Callable[[dict], None] | None = None,
                 group_pipeline: bool = True,
                 operation_token: str = "") -> tuple[list, dict]:
        """兼容入口；内部阶段统一通过 :class:`OrganizeContext` 传参。"""
        context = OrganizeContext(
            source_dir_id=str(source_dir_id),
            dry_run=bool(dry_run),
            # 保留历史兼容语义：负数会在扫描入口立即停止，而不是被解释为“不限”。
            max_files=int(max_files or 0),
            cancel_event=cancel_event,
            post_actions=bool(post_actions),
            source_name=str(source_name or ""),
            require_complete_scan=bool(require_complete_scan),
            media_probe_cache_only=media_probe_cache_only,
            protected_source_ids=frozenset(
                str(item).strip()
                for item in (protected_source_ids or set())
                if str(item).strip()
            ),
            automatic=bool(automatic),
            group_progress=group_progress,
            group_pipeline=bool(group_pipeline),
            operation_token=str(operation_token or "").strip(),
        )
        return self._organize(context, rules)

    def _group_pipeline_enabled(self, context: OrganizeContext) -> bool:
        """预览仍走整源路径：跨组统一冲突仲裁对只读预演更有价值。

        ``max_files`` 是整源级预览截断语义（负数立即停止、正数拒绝写入），
        与逐组枚举不兼容；声明 ``supports_group_pipeline = False`` 的作用域
        客户端只允许一次自顶向下扫描，同样必须继续沿用旧路径。
        """
        if context.dry_run or not context.group_pipeline or context.max_files:
            return False
        if not bool(getattr(self.client, "supports_group_pipeline", True)):
            return False
        return get_bool("ORGANIZE_GROUP_PIPELINE", True)

    def _reset_task_caches(self) -> None:
        """任务级缓存必须逐任务重置，禁止跨任务继承过期快照。"""
        # 受控强制刷新是“每次整理任务、每个 TMDB 身份最多一次”，不能跨任务继承。
        self._forced_detail_refreshes.clear()
        # 一次整理会在预仲裁和执行前复核两次同一目标，媒体探测缓存只需读取一次。
        self._existing_variant_cache.clear()
        self._media_probe_payload_cache.clear()
        self._media_probe_cache_checked.clear()
        from app.modules.media_probe import ProbeBudget
        self._probe_budget = ProbeBudget(24)

    def _organize(
        self, context: OrganizeContext, rules: OrganizeRules,
    ) -> tuple[list, dict]:
        """按扫描、规划、冲突预演和串行执行阶段完成一次整理。"""
        rules = enforce_fixed_organize_rules(rules)
        if self._group_pipeline_enabled(context):
            return self._organize_groups(context, rules)
        source_dir_id = context.source_dir_id
        total_started = time.monotonic()
        performance_before = self._read_performance_snapshot()
        self._reset_task_caches()
        stats = self._initial_stats()
        scan_result = self._scan_source(context, rules, stats)
        # 写入任务在识别/TMDB/AI 阶段前即拒绝不完整扫描，既避免基于部分
        # 快照规划云盘变更，也避免为注定不能执行的任务继续消耗外部请求。
        self._validate_scan_for_execution(context, stats)

        planning_result = self._build_plans(
            scan_result, context, rules, stats, performance_before,
        )
        plans = planning_result.plans

        self._run_execution_stage(
            scan_result, planning_result, context, rules, stats,
        )
        stats["total_elapsed_seconds"] = round(time.monotonic() - total_started, 3)
        if self._probe_budget is not None:
            stats["media_probe_attempts"] = self._probe_budget.attempted
            stats["media_probe_failure_cache_hits"] = self._probe_budget.failure_cache_hits
            stats["media_probe_budget_skipped"] = self._probe_budget.skipped_by_budget
            stats["media_probe_timeouts"] = self._probe_budget.timeouts
        logger.info(
            "整理阶段耗时 source=%s %s", source_dir_id, _format_phase_timing(stats)
        )
        return plans, stats

    # ===== 媒体组流水线 =====
    @staticmethod
    def _group_restriction(task: OrganizeGroupTask) -> ScanRestriction:
        """把扫描限制在单个媒体组内，深层目录继续继承本组身份。"""
        if task.is_root:
            return ScanRestriction(
                dir_id=task.source_dir_id,
                rel="",
                files_only=True,
                group_id=task.source_dir_id,
                group_path=GROUP_ROOT_PATH,
            )
        return ScanRestriction(
            dir_id=task.group_id,
            rel=task.group_path,
            group_id=task.group_id,
            group_path=task.group_path,
            etag=task.etag,
            updated_at=task.updated_at,
        )

    def _publish_group_progress(
        self, context: OrganizeContext, stats: dict, progress: GroupProgress,
    ) -> None:
        """实时投影当前媒体组与阶段；观测失败不得影响整理结果。"""
        snapshot = progress.to_dict()
        stats["group_progress"] = snapshot
        callback = context.group_progress
        if callback is None:
            return
        try:
            callback({
                "progress": snapshot,
                "groups": [dict(row) for row in (stats.get("source_groups") or [])],
            })
        except Exception:
            logger.debug("媒体组进度回调失败", exc_info=True)

    def _organize_groups(
        self, context: OrganizeContext, rules: OrganizeRules,
    ) -> tuple[list, dict]:
        """逐媒体组完成扫描、识别、冲突预检与执行。

        单组内保留完整目录上下文与批内仲裁，不退化为逐文件盲目移动；
        组间串行且失败隔离，第一个媒体目录无需等待后续组的识别耗时。
        """
        total_started = time.monotonic()
        self._reset_task_caches()
        stats = self._initial_stats()
        enumeration = enumerate_group_tasks(
            self.client,
            source_dir_id=context.source_dir_id,
            source_name=context.source_name,
            video_exts=self.video_exts(rules),
            protected_source_ids=set(context.protected_source_ids),
            trigger="automatic" if context.automatic else "manual",
            cancelled=context.cancelled,
        )
        if not enumeration.complete:
            # 部分列表不得冒充完整枚举：漏掉的媒体组会让后续清理误判来源已空。
            stats["scan_complete"] = False
            for message in enumeration.errors:
                if message not in stats["scan_errors"]:
                    stats["scan_errors"].append(message)
            if context.cancelled():
                stats["stopped"] = 1
                return [], stats
            raise OrganizeScanUnsafeError(
                "目录扫描不完整，已在首次云盘写入前终止，请稍后重试"
            )

        tasks = enumeration.tasks
        rows: dict[str, dict[str, object]] = {
            task.key: {
                "id": task.group_id,
                "path": task.group_path,
                "name": task.group_name,
                "status": "planned",
                "stage": GROUP_STAGE_PENDING,
                "index": task.index,
                "total": 0,
                "moved": 0,
                "metadata_moved": 0,
                "skipped": 0,
                "need_confirm": 0,
                "failed": 0,
            }
            for task in tasks
        }
        stats["source_groups"] = list(rows.values())
        stats["source_groups_total"] = len(tasks)
        progress = GroupProgress(total=len(tasks), started_at=time.monotonic())
        self._publish_group_progress(context, stats, progress)

        group_context = replace(context, post_actions=False)
        probe_elapsed_total = 0.0
        probe_cap_reported = False
        plans: list[OrganizePlan] = []
        results: list[OrganizeGroupResult] = []
        for task in tasks:
            row = rows[task.key]
            if context.cancelled():
                stats["stopped"] = 1
                row["status"] = GROUP_STATUS_STOPPED
                row["stage"] = GROUP_STAGE_DONE
                continue

            def _stage(name: str, _row: dict = row) -> None:
                progress.current_stage = name
                _row["stage"] = name
                self._publish_group_progress(context, stats, progress)

            def _file_progress(index: int, total: int) -> None:
                progress.current_file_index = index
                progress.current_file_total = total
                self._publish_group_progress(context, stats, progress)

            progress.current_index = task.index
            progress.current_group = task.group_name
            progress.current_file_index = 0
            progress.current_file_total = 0
            row["status"] = GROUP_STATUS_RUNNING
            stats["current_source_group"] = task.group_name
            _stage(GROUP_STAGE_SCAN)

            group_stats = self._initial_stats()
            error = ""
            started = time.monotonic()
            # 上一组的移动会改变目标目录内容，目标列表缓存必须逐组失效。
            self._existing_variant_cache.clear()
            performance_before = self._read_performance_snapshot()
            # 每组各有独立探测预算（整源旧路径是全源共享 60s）。为防止
            # 病态来源按组数放大探测墙钟，累计耗时到达来源级上限后，
            # 剩余组只读探测缓存，命名信息按已有缓存与文件名降级。
            run_context = group_context
            if probe_elapsed_total >= _GROUP_PIPELINE_PROBE_CAP_SECONDS:
                if not probe_cap_reported:
                    probe_cap_reported = True
                    logger.warning(
                        "媒体探测累计耗时达到来源级上限 %.0fs，剩余媒体组改用缓存探测",
                        _GROUP_PIPELINE_PROBE_CAP_SECONDS,
                    )
                run_context = replace(group_context, media_probe_cache_only=True)
            try:
                scan_result = self._scan_source(
                    run_context, rules, group_stats,
                    restriction=self._group_restriction(task),
                )
                # 组内写入前即拒绝不完整快照，避免基于部分目录规划云盘变更。
                self._validate_scan_for_execution(run_context, group_stats)
                _stage(GROUP_STAGE_PLAN)
                planning_result = self._build_plans(
                    scan_result, run_context, rules, group_stats, performance_before,
                )
                plans.extend(planning_result.plans)
                self._run_execution_stage(
                    scan_result, planning_result, run_context, rules, group_stats,
                    on_progress=_file_progress, on_stage=_stage,
                )
            except OrganizeScanUnsafeError:
                # 快照不可信属于来源级安全门，禁止被组级隔离降级为部分成功。
                merge_group_stats(stats, group_stats)
                stats["source_groups"] = list(rows.values())
                stats["current_source_group"] = ""
                raise
            except Exception as exc:
                # 组间失败隔离：当前组停止剩余危险写入，后续组继续执行。
                error = _safe_organize_failure(exc)
                group_stats["failed"] = int(group_stats.get("failed", 0) or 0) or 1
                logger.exception(
                    "媒体组整理失败 source=%s group=%s",
                    context.source_dir_id, task.group_path,
                )
            if self._probe_budget is not None:
                group_stats["media_probe_attempts"] = self._probe_budget.attempted
                group_stats["media_probe_failure_cache_hits"] = (
                    self._probe_budget.failure_cache_hits
                )
                group_stats["media_probe_budget_skipped"] = (
                    self._probe_budget.skipped_by_budget
                )
                group_stats["media_probe_timeouts"] = self._probe_budget.timeouts

            result = build_group_result(
                task, group_stats, error=error,
                elapsed_seconds=round(time.monotonic() - started, 3),
            )
            results.append(result)
            probe_elapsed_total += float(
                group_stats.get("media_probe_elapsed_seconds") or 0.0
            )
            if probe_cap_reported:
                group_stats["media_probe_wall_clock_capped"] = 1
            merge_group_stats(stats, group_stats)
            row.update(result.progress_row())
            stats["source_groups"] = list(rows.values())
            stats["source_groups_completed"] = sum(
                1 for item in rows.values()
                if is_terminal_group_status(str(item.get("status") or ""))
            )
            progress.completed = int(stats["source_groups_completed"])
            _stage(GROUP_STAGE_DONE)
            logger.info(
                "媒体组完成 source=%s group=%s(%s/%s) status=%s moved=%s failed=%s %.3fs",
                context.source_dir_id, task.group_path, task.index, task.total,
                result.status, result.moved, result.failed, result.elapsed_seconds,
            )

        stats["group_results"] = [item.to_dict() for item in results]
        stats["source_groups"] = list(rows.values())
        stats["source_groups_completed"] = sum(
            1 for item in rows.values()
            if is_terminal_group_status(str(item.get("status") or ""))
        )
        stats["current_source_group"] = ""
        progress.completed = int(stats["source_groups_completed"])
        progress.current_group = ""
        progress.current_stage = GROUP_STAGE_DONE
        self._publish_group_progress(context, stats, progress)

        if context.post_actions and not context.cancelled():
            self._run_post_actions(stats, rules, source_name=context.source_name)
        stats["total_elapsed_seconds"] = round(time.monotonic() - total_started, 3)
        logger.info(
            "媒体组流水线完成 source=%s groups=%s/%s moved=%s failed=%s total=%.3fs",
            context.source_dir_id,
            int(stats.get("source_groups_completed", 0) or 0),
            len(tasks),
            int(stats.get("moved", 0) or 0),
            int(stats.get("failed", 0) or 0),
            float(stats.get("total_elapsed_seconds", 0.0) or 0.0),
        )
        return plans, stats

    @staticmethod
    def trigger_post_actions(
        stats: dict, rules: OrganizeRules, source_name: str = "", chat_id: str = "",
        download_request_ids: list[int] | None = None,
        *, notify_result: bool = True,
        strm_debounce_seconds: float | None = None,
        notification_threads: list[dict[str, object]] | None = None,
    ) -> None:
        """多源任务聚合完成后只执行一次 STRM 联动和总结通知。"""
        unsafe_partial = bool(
            stats.get("failed")
            or stats.get("scan_errors")
            or stats.get("replacement_cleanup_failed")
            or stats.get("empty_dir_cleanup_failed")
            or stats.get("source_dir_cleanup_failed")
            or stats.get("audit_failures")
        )
        if (
            rules.link_strm and not stats.get("stopped") and stats.get("moved", 0)
            and (not unsafe_partial or stats.get("strm_changes"))
        ):
            resolved_debounce = strm_debounce_seconds
            try:
                has_pending_confirmation = int(
                    stats.get("need_confirm", 0) or 0
                ) > 0
            except (TypeError, ValueError, OverflowError):
                has_pending_confirmation = False
            if resolved_debounce is None and has_pending_confirmation:
                resolved_debounce = get(
                    "GY_ORGANIZE_PENDING_STRM_DEBOUNCE_SECONDS", "30"
                ) or 30
            try:
                normalized_debounce = max(
                    0.0, min(float(resolved_debounce or 0.0), 120.0)
                )
            except (TypeError, ValueError, OverflowError):
                normalized_debounce = 30.0 if has_pending_confirmation else 0.0
            Organizer._post_organize_link(
                stats, rules, download_request_ids=download_request_ids,
                chat_id=chat_id, force_incremental=unsafe_partial,
                debounce_seconds=normalized_debounce,
                notification_threads=notification_threads,
            )
        elif rules.link_strm and unsafe_partial:
            stats["strm"] = {
                "ok": False,
                "skipped": True,
                "error": "整理存在失败项且没有已确认的变更清单，已停止 STRM 同步",
            }
        if notify_result:
            Organizer._notify_result(
                stats, rules, source_name=source_name, chat_id=chat_id
            )

    @staticmethod
    def _post_organize_link(
        stats: dict, rules: OrganizeRules, *,
        download_request_ids: list[int] | None = None,
        chat_id: str = "",
        force_incremental: bool = False,
        debounce_seconds: float = 0.0,
        notification_threads: list[dict[str, object]] | None = None,
    ) -> None:
        """整理后联动：触发 STRM；媒体库刷新由 STRM 完成后统一处理。"""
        base_url = get("GY_STRM_BASE_URL", "")
        strm_root = get("STRM_ROOT", "")
        if base_url and strm_root:
            try:
                from app.modules.scheduler import get_scheduler
                linked_threads = (
                    list(notification_threads or [])
                    or ([{
                        "topic": "organize",
                        "thread_key": f"organize:{str(stats.get('task_id') or '')}",
                        "task_id": str(stats.get("task_id") or ""),
                        "chat_id": str(chat_id or ""),
                        "topic_enabled": bool(rules.notify_enabled and rules.library_notify),
                    }] if str(stats.get("task_id") or "").strip() and not download_request_ids else [])
                )
                trigger_options = {
                    "notify_override": rules.notify_enabled,
                    "detail_notify_override": rules.notify_enabled and rules.strm_detail_notify,
                    "emby_refresh_override": rules.emby_refresh,
                    "download_request_ids": download_request_ids,
                    "organize_changes": list(stats.get("strm_changes") or []),
                    "force_full": False if force_incremental else (
                        bool(stats.get("strm_force_full")) or not bool(stats.get("strm_changes"))
                    ),
                }
                if linked_threads:
                    trigger_options["notification_threads"] = linked_threads
                if chat_id:
                    trigger_options["chat_id"] = chat_id
                if debounce_seconds > 0:
                    trigger_options["debounce_seconds"] = debounce_seconds
                result = get_scheduler().trigger("organize", **trigger_options)
                logger.info(f"整理联动 STRM: {result}")
                stats["strm"] = result
            except Exception as e:
                logger.error("联动 STRM 失败 type=%s", type(e).__name__)
                stats["strm"] = {
                    "ok": False, "error_code": "strm_trigger_failed",
                    "error": f"STRM 联动启动失败: {str(e)[:300]}",
                }


    def _apply_nsfw_multipart_policy(
        self, plans: list[OrganizePlan], rules: OrganizeRules,
    ) -> None:
        """只对成人专用来源处理明确分段；无法排序时回退人工确认。"""
        if not rules.nsfw_exclusive:
            return
        from app.modules.nsfw import extract_nsfw_identifier, extract_nsfw_multipart

        grouped: dict[tuple[str, str], list[tuple[OrganizePlan, object | None]]] = {}
        for plan in plans:
            identifier = extract_nsfw_identifier(
                plan.original_name, rules.nsfw_strip_domains,
            )
            if identifier is None:
                continue
            multipart = extract_nsfw_multipart(
                plan.original_name, rules.nsfw_strip_domains,
            )
            if multipart is not None and plan.multipart_index is None:
                plan.multipart_index = multipart.part_index
                plan.multipart_token = multipart.token
                plan.multipart_ambiguous = multipart.ambiguous
            grouped.setdefault(
                (str(plan.original_path or "/"), identifier.code), []
            ).append((plan, multipart))

        for members in grouped.values():
            if len(members) < 2:
                continue
            has_explicit_part = any(item is not None for _plan, item in members)
            if not has_explicit_part:
                # 多版本文件并不等于多分段，继续交给既有版本冲突策略。
                continue
            ambiguous = any(
                item is None or bool(getattr(item, "ambiguous", False))
                for _plan, item in members
            )
            if not ambiguous:
                continue
            message = "同一番号包含多个视频，但无法安全确定分段顺序，请人工确认"
            for plan, _multipart in members:
                if plan.match is None:
                    plan.match = self._nsfw_unresolved_match()
                plan.match.need_confirm = True
                plan.match.status = "low_confidence"
                plan.match.error = message
                plan.match.metadata = {
                    **dict(getattr(plan.match, "metadata", None) or {}),
                    "nsfw_multipart_confirmation": True,
                }
                plan.action = "skip"
                plan.note = message
                plan.multipart_ambiguous = True

    def _build_confirmation_groups(
        self,
        plans: list[OrganizePlan],
        companion_files: dict[str, list[GuangYaFile]],
        *,
        source_dir_id: str,
        source_name: str,
        rules: OrganizeRules | None = None,
    ) -> list[dict]:
        """把逐文件低置信度结果聚合为可由 Telegram 一次确认的媒体组。"""
        groups: dict[tuple[str, str], dict] = {}
        for plan in plans:
            match = plan.match
            if plan.action != "skip" or match is None or not match.need_confirm:
                continue
            context = getattr(match, "context", None)
            identity = str(getattr(context, "normalized_title", "") or "").strip()
            if rules is not None and rules.nsfw_exclusive:
                from app.modules.nsfw import extract_nsfw_identifier
                identifier = extract_nsfw_identifier(
                    plan.original_name, rules.nsfw_strip_domains,
                )
                if identifier is not None:
                    identity = identifier.code
            if not identity:
                identity = str(match.title or plan.original_path or plan.original_name).strip()
            identity_key = _normalize_media_identity(identity) or str(plan.file_id)
            key = (str(plan.original_path or "/"), identity_key)
            group = groups.setdefault(key, {
                "source_dir_id": str(source_dir_id),
                "source_name": str(source_name or ""),
                "directory": str(plan.original_path or "/"),
                "source_parent_id": str(plan.original_parent_id or "0"),
                "identity": identity,
                "reason": str(match.error or "识别结果需人工确认"),
                "rules": organize_rules_snapshot(rules) if rules is not None else None,
                "files": [],
                "companions": [],
                "_candidate_map": {},
                "multipart_strategy": "",
            })
            if bool(
                dict(getattr(match, "metadata", None) or {}).get(
                    "nsfw_multipart_confirmation"
                )
            ):
                group["multipart_strategy"] = "sequence"
            season = episode = None
            parse_source_position = getattr(self.scraper, "parse_source_position", None)
            if callable(parse_source_position):
                try:
                    season, episode = parse_source_position(
                        plan.original_name, plan.original_path
                    )
                except Exception:
                    pass
            if season is None:
                season = getattr(context, "season", None)
            if episode is None:
                episode = getattr(context, "episode", None)
            group["files"].append({
                "file_id": str(plan.file_id),
                "name": str(plan.original_name),
                "parent_id": str(plan.original_parent_id or "0"),
                "size": int(plan.size or 0),
                "etag": str(plan.etag or ""),
                "season": season,
                "episode": episode,
                "multipart_index": plan.multipart_index,
                "multipart_token": plan.multipart_token,
            })
            existing_companions = {str(item["file_id"]) for item in group["companions"]}
            for companion in self._companions_for_plan(
                plan, companion_files.get(plan.original_path, [])
            ):
                if str(companion.file_id) in existing_companions:
                    continue
                group["companions"].append({
                    "file_id": str(companion.file_id),
                    "name": str(companion.name),
                    "parent_id": str(companion.parent_id or plan.original_parent_id or "0"),
                    "size": int(companion.size or 0),
                    "etag": str(companion.etag or ""),
                })
                existing_companions.add(str(companion.file_id))

            candidates = list(match.candidates or [])
            match_provider = self._match_provider(match)
            match_external_id = self._match_external_id(match)
            if match_external_id and not any(
                self._match_provider(item) == match_provider
                and self._match_external_id(item) == match_external_id
                and str(getattr(item, "media_type", "") or match.media_type or "")
                == str(match.media_type or "")
                for item in candidates
            ):
                candidates.insert(0, match)
            candidate_map = group["_candidate_map"]
            for candidate in candidates[:5]:
                tmdb_id = str(getattr(candidate, "tmdb_id", "") or "").strip()
                provider = str(getattr(candidate, "provider", "") or "").strip().lower()
                external_id = str(
                    getattr(candidate, "external_id", "") or tmdb_id
                ).strip()
                if not provider and tmdb_id:
                    provider = "tmdb"
                if provider == "tmdb":
                    external_id = tmdb_id
                media_type = str(
                    getattr(candidate, "media_type", "") or match.media_type or ""
                ).strip()
                valid_identity = (
                    (provider == "tmdb" and bool(tmdb_id))
                    or (
                        provider in {"metatube", "clean_title"}
                        and bool(external_id)
                    )
                )
                if not valid_identity or media_type not in {"movie", "tv"}:
                    continue
                if provider in {"metatube", "clean_title"} and media_type != "movie":
                    continue
                candidate_key = (provider, external_id, media_type)
                score = float(
                    getattr(candidate, "score", getattr(candidate, "confidence", 0.0)) or 0.0
                )
                metadata = getattr(candidate, "metadata", None) or {}
                genre_ids = [
                    int(value) for value in (metadata.get("genre_ids") or [])
                    if str(value).isdigit()
                ] if isinstance(metadata, dict) else []
                current = candidate_map.setdefault(candidate_key, {
                    "provider": provider,
                    "external_id": external_id,
                    "tmdb_id": tmdb_id if provider == "tmdb" else "",
                    "media_type": media_type,
                    "title": str(getattr(candidate, "title", "") or match.title or ""),
                    "year": str(getattr(candidate, "year", "") or match.year or ""),
                    "genre_ids": genre_ids,
                    "scores": [],
                    "support": 0,
                })
                if not current.get("genre_ids") and genre_ids:
                    current["genre_ids"] = genre_ids
                current["scores"].append(max(0.0, min(score, 1.0)))
                current["support"] += 1

        result: list[dict] = []
        for group in groups.values():
            candidates = []
            for candidate in group.pop("_candidate_map").values():
                scores = list(candidate.pop("scores") or [0.0])
                candidate["score"] = sum(scores) / len(scores)
                candidates.append(candidate)
            candidates.sort(
                key=lambda item: (
                    -int(item.get("support") or 0),
                    -float(item.get("score") or 0.0),
                    str(item.get("title") or "").casefold(),
                )
            )
            if rules is not None and rules.nsfw_exclusive and not candidates:
                from app.modules.nsfw import build_clean_title_candidate
                seed = next((
                    str(item.get("name") or "")
                    for item in (group.get("files") or [])
                    if str(item.get("name") or "").strip()
                ), str(group.get("identity") or ""))
                fallback = build_clean_title_candidate(
                    seed, rules.nsfw_strip_domains,
                )
                if fallback is not None:
                    fallback["support"] = len(group.get("files") or [])
                    candidates.append(fallback)
            # 无有效番号时仍保留确认组并提供“跳过此组”终态；明确番号但
            # MetaTube 无结果时，优先提供“清洗标题后入库”。
            group["candidates"] = candidates[:3]
            result.append(group)
        return result

    @staticmethod
    def _notification_downstream_labels(stats: dict) -> tuple[str, str]:
        """把整理后处理结果统一映射为 STRM 与媒体库通知状态。"""
        strm = stats.get("strm") if isinstance(stats.get("strm"), dict) else {}
        if strm.get("ok"):
            return "已排队", "等待 STRM 完成"
        if strm.get("skipped"):
            return "已跳过", "未触发"
        if strm:
            return "启动失败", "未触发"
        return "未启用或无变更", "未触发"

    @staticmethod
    def notify_directory_results(stats: dict, rules: OrganizeRules,
                                 source_name: str = "", chat_id: str = "") -> None:
        """目录刮削只发一条汇总；人工候选继续使用独立可更新按钮卡。"""
        if not rules.notify_enabled or not rules.library_notify:
            return
        try:
            from app.modules.telegram_organize_lifecycle import publish_organize_lifecycle
            from app.notifier import safe_int

            task_id = str(
                stats.get("task_id") or stats.get("operation_token") or ""
            ).strip() or f"directory-{time.time_ns()}"
            # 目录刮削的首次汇总与随后 STRM 状态必须复用同一个线程。
            # 写回 stats 后，trigger_post_actions() 的兼容汇总不会再生成 legacy-*。
            stats["task_id"] = task_id
            groups, actionable_count = Organizer._validated_task_confirmation_groups(stats)
            notification_stats = {
                **stats,
                "notification_actionable_confirmation_files": actionable_count,
                "notification_actionable_confirmation_groups": len(groups),
            }
            unsafe_partial = bool(
                stats.get("failed")
                or stats.get("scan_errors")
                or stats.get("replacement_cleanup_failed")
                or stats.get("empty_dir_cleanup_failed")
                or stats.get("source_dir_cleanup_failed")
                or stats.get("audit_failures")
            )
            waiting_for_strm = bool(
                rules.link_strm
                and not stats.get("stopped")
                and safe_int(stats.get("moved"), 0, minimum=0)
                and (not unsafe_partial or stats.get("strm_changes"))
            )
            publish_organize_lifecycle(
                task_id, notification_stats, source_name=source_name or "目录刮削",
                chat_id=chat_id,
                topic_enabled=rules.notify_enabled and rules.library_notify,
                strm_status=(
                    "等待后处理" if waiting_for_strm
                    else "未启用或无变更" if rules.link_strm
                    else "未启用"
                ),
                media_refresh=(
                    "等待 STRM 完成" if waiting_for_strm else "未触发"
                ),
            )
            Organizer._deliver_task_confirmation_groups(
                groups, rules, source_name=source_name, chat_id=chat_id,
            )
        except Exception as exc:
            logger.warning("整理目录通知失败 type=%s", type(exc).__name__)

    @staticmethod
    def notify_task_results(stats: dict, rules: OrganizeRules,
                            source_name: str = "", chat_id: str = "") -> bool:
        """发送一条可被 STRM 终态继续更新的整理事务消息，并独立保留候选卡。"""
        if not rules.notify_enabled or not rules.library_notify:
            return False
        try:
            from app.modules.telegram_organize_lifecycle import (
                publish_organize_lifecycle,
            )

            task_id = str(stats.get("task_id") or "").strip()
            if not task_id:
                # 兼容旧插件直接调用；同一 stats 被重复调用时仍复用线程。
                task_id = f"legacy-{time.time_ns()}"
                stats["task_id"] = task_id
            strm_status, refresh_status = Organizer._notification_downstream_labels(
                stats
            )
            groups, actionable_count = (
                Organizer._validated_task_confirmation_groups(stats)
            )
            candidate_group_count = sum(
                1 for group in groups if list(group.get("candidates") or [])
            )
            skip_only_group_count = len(groups) - candidate_group_count
            notification_stats = {
                **stats,
                "notification_actionable_confirmation_files": actionable_count,
                "notification_actionable_confirmation_groups": len(groups),
                "notification_candidate_confirmation_groups": candidate_group_count,
                "notification_skip_confirmation_groups": skip_only_group_count,
            }
            summary_sent = bool(publish_organize_lifecycle(
                task_id,
                notification_stats,
                source_name=source_name,
                chat_id=chat_id,
                topic_enabled=rules.notify_enabled and rules.library_notify,
                strm_status=strm_status,
                media_refresh=refresh_status,
            ))
            confirmations_sent = Organizer._deliver_task_confirmation_groups(
                groups,
                rules,
                source_name=source_name,
                chat_id=chat_id,
            )
            return bool(summary_sent and confirmations_sent)
        except Exception as exc:
            logger.warning("整理任务汇总通知失败 type=%s", type(exc).__name__)
            return False

    @staticmethod
    def _validated_task_confirmation_groups(
        stats: dict,
    ) -> tuple[list[dict], int]:
        """校验确认快照，避免生成无法安全重跑的 Telegram 操作按钮。"""
        raw_groups = [
            item for item in (stats.get("confirmation_groups") or [])
            if isinstance(item, dict)
        ]
        groups: list[dict] = []
        actionable_file_keys: set[str] = set()
        for group_index, group in enumerate(raw_groups):
            def valid_candidate(item: object) -> bool:
                if not isinstance(item, dict):
                    return False
                provider = str(item.get("provider") or "").strip().lower()
                tmdb_id = str(item.get("tmdb_id") or "").strip()
                external_id = str(item.get("external_id") or tmdb_id).strip()
                if not provider and tmdb_id:
                    provider = "tmdb"
                media_type = str(item.get("media_type") or "").strip().lower()
                if provider == "tmdb":
                    return bool(tmdb_id and media_type in {"movie", "tv"})
                if provider in {"metatube", "clean_title"}:
                    return bool(external_id and media_type == "movie")
                return False

            valid_candidates = [
                dict(item) for item in (group.get("candidates") or [])
                if valid_candidate(item)
            ]
            raw_files = list(group.get("files") or [])
            raw_companions = list(group.get("companions") or [])
            source_parent_id = str(group.get("source_parent_id") or "0")

            def valid_snapshot(item: object, *, require_scope: bool) -> bool:
                if not isinstance(item, dict):
                    return False
                if not str(item.get("file_id") or "").strip():
                    return False
                if not str(item.get("name") or "").strip():
                    return False
                parent_id = str(item.get("parent_id") or "0")
                if require_scope and parent_id != source_parent_id:
                    return False
                try:
                    return int(item.get("size") or 0) >= 0
                except (TypeError, ValueError):
                    return False

            valid_files = [
                dict(item) for item in raw_files
                if valid_snapshot(item, require_scope=True)
            ]
            valid_companions = [
                dict(item) for item in raw_companions
                if valid_snapshot(item, require_scope=False)
            ]
            if (
                not valid_files
                or len(valid_files) != len(raw_files)
                or len(valid_companions) != len(raw_companions)
            ):
                continue
            groups.append({
                **group,
                "files": valid_files,
                "companions": valid_companions,
                "candidates": valid_candidates,
            })
            for file_index, file in enumerate(valid_files):
                file_id = str(file.get("file_id") or "").strip()
                actionable_file_keys.add(
                    f"id:{file_id}"
                    if file_id else f"row:{group_index}:{file_index}"
                )
        return groups, len(actionable_file_keys)

    @staticmethod
    def _deliver_task_confirmation_groups(
        groups: list[dict],
        rules: OrganizeRules,
        *,
        source_name: str = "",
        chat_id: str = "",
    ) -> bool:
        """发送独立候选卡；失败时保留已持久化任务并给出 Web 回退。"""
        if not groups:
            return True
        from app.modules.organize_confirmations import (
            confirmation_event, publish_confirmation_event,
        )

        confirmation_failures = 0
        for index, group in enumerate(groups, start=1):
            group_source = str(group.get("source_name") or source_name)
            scope = str(group.get("directory") or "/")
            if group_source and scope != "/":
                scope = f"{group_source}/{scope}"
            elif group_source:
                scope = group_source
            try:
                event = confirmation_event(
                    f"⚠️ 待确认媒体 {index}/{len(groups)}",
                    {
                        "媒体": str(group.get("identity") or "待确认媒体"),
                        "剧集": Organizer._confirmation_scope_summary(group),
                        "来源": scope,
                    },
                    group,
                    rules,
                    source_name=group_source,
                    chat_id=chat_id,
                )
                delivered = publish_confirmation_event(event, chat_id=chat_id)
            except Exception as exc:
                delivered = False
                logger.warning(
                    "整理待确认卡发送失败 index=%s type=%s",
                    index,
                    type(exc).__name__,
                )
            if not delivered:
                confirmation_failures += 1
        if confirmation_failures:
            logger.warning(
                "整理待确认卡未被通知中心接纳 count=%s；候选仍保留在 Web 待确认队列",
                confirmation_failures,
            )
        return confirmation_failures == 0

    @staticmethod
    def notify_task_confirmations(
        stats: dict,
        rules: OrganizeRules,
        source_name: str = "",
        chat_id: str = "",
    ) -> bool:
        """只发送待确认候选卡，不重复发送任务汇总或媒体入库卡。"""
        if not rules.notify_enabled or not rules.library_notify:
            return False
        try:
            groups, _actionable_count = (
                Organizer._validated_task_confirmation_groups(stats)
            )
            return Organizer._deliver_task_confirmation_groups(
                groups,
                rules,
                source_name=source_name,
                chat_id=chat_id,
            )
        except Exception as exc:
            logger.warning("整理待确认通知失败 type=%s", type(exc).__name__)
            return False

    @staticmethod
    def _notify_result(stats: dict, rules: OrganizeRules,
                       source_name: str = "", chat_id: str = "") -> None:
        """兼容旧插件的汇总入口；统一转入整理事务消息。"""
        if not rules.notify_enabled or not rules.library_notify:
            return
        try:
            from app.modules.telegram_organize_lifecycle import publish_organize_lifecycle

            task_id = str(stats.get("task_id") or "").strip()
            if not task_id:
                task_id = f"legacy-{time.time_ns()}"
                stats["task_id"] = task_id
            strm_status, media_refresh = Organizer._notification_downstream_labels(
                stats
            )
            publish_organize_lifecycle(
                task_id, stats, source_name=source_name, chat_id=chat_id,
                topic_enabled=rules.notify_enabled and rules.library_notify,
                strm_status=strm_status, media_refresh=media_refresh,
            )
        except Exception as exc:
            logger.warning("整理通知失败 type=%s", type(exc).__name__)

    @staticmethod
    def _notification_count_summary(counts: dict, *, compact: bool = False) -> str:
        from app.notifier import safe_int

        total = safe_int(
            counts.get("视频", counts.get("总视频", 0)), 0, minimum=0
        )
        suffix = "" if compact else " 个"
        parts = [f"视频 {total}{suffix}"]
        labels = (
            ("已移动", "入库" if compact else "成功入库"),
            ("元数据", "元数据"),
            ("需确认", "待确认" if compact else "需要确认"),
            ("跳过", "跳过"),
            ("失败", "失败"),
        )
        for key, label in labels:
            value = safe_int(counts.get(key, 0), 0, minimum=0)
            if value > 0:
                parts.append(f"{label} {value}{suffix}")
        return " · ".join(parts)

    @staticmethod
    def _confirmation_scope_summary(group: dict) -> str:
        from app.notifier import safe_int

        files = [
            item for item in (group.get("files") or []) if isinstance(item, dict)
        ]
        seasons = sorted({
            safe_int(item.get("season"), 0, minimum=0)
            for item in files if item.get("season") not in (None, "")
        })
        episodes = sorted({
            safe_int(item.get("episode"), 0, minimum=0)
            for item in files if item.get("episode") not in (None, "")
        })
        parts: list[str] = []
        if len(seasons) == 1:
            parts.append("特别篇" if seasons[0] == 0 else f"第 {seasons[0]} 季")
        elif len(seasons) > 1:
            parts.append(f"{len(seasons)} 个季度")
        if episodes:
            ranges: list[str] = []
            start = previous = episodes[0]
            for episode in episodes[1:]:
                if episode == previous + 1:
                    previous = episode
                    continue
                ranges.append(
                    f"E{start:02d}" if start == previous
                    else f"E{start:02d}–E{previous:02d}"
                )
                start = previous = episode
            ranges.append(
                f"E{start:02d}" if start == previous
                else f"E{start:02d}–E{previous:02d}"
            )
            parts.append("、".join(ranges))
        parts.append(f"共 {len(files)} 个视频")
        return " · ".join(parts)

    @staticmethod
    def _append_reason(container: dict, key: str, reason: object, *, limit: int = 20) -> None:
        text = str(reason or "").strip()
        if not text:
            return
        reasons = container.setdefault(key, [])
        if not isinstance(reasons, list):
            return
        if text not in reasons and len(reasons) < limit:
            reasons.append(text)

    @staticmethod
    def _task_notification_footer(
        stats: dict,
        *,
        confirmation_group_count: int = 0,
        actionable_confirmation_count: int | None = None,
        candidate_group_count: int | None = None,
        skip_only_group_count: int | None = None,
    ) -> str:
        """生成任务级提示，区分候选卡、无元数据跳过卡与不可操作项。"""
        from app.notifier import safe_int

        sections: list[str] = []
        need_confirm = safe_int(stats.get("need_confirm", 0), 0, minimum=0)
        actionable = min(
            need_confirm,
            (
                need_confirm
                if actionable_confirmation_count is None and confirmation_group_count > 0
                else safe_int(actionable_confirmation_count, 0, minimum=0)
            ),
        )
        candidate_groups = max(0, min(
            confirmation_group_count,
            confirmation_group_count if candidate_group_count is None
            else safe_int(candidate_group_count, 0, minimum=0),
        ))
        skip_only_groups = max(0, min(
            confirmation_group_count - candidate_groups,
            (confirmation_group_count - candidate_groups)
            if skip_only_group_count is None
            else safe_int(skip_only_group_count, 0, minimum=0),
        ))
        if need_confirm:
            if confirmation_group_count > 0:
                lines = [
                    (
                        f"⚠️ 待确认 {need_confirm} 个文件"
                        if need_confirm != confirmation_group_count
                        else f"⚠️ 待确认 {confirmation_group_count} 组"
                    ),
                    (
                        f"已按媒体合并为 {confirmation_group_count} 组，"
                        f"下方将发送 {confirmation_group_count} 张处理卡。"
                    ),
                ]
                if candidate_groups:
                    lines.append(f"• {candidate_groups} 组可直接选择识别候选")
                if skip_only_groups:
                    lines.append(
                        f"• {skip_only_groups} 组暂无可用元数据，可在 Telegram 直接跳过"
                    )
                without_cards = max(0, need_confirm - actionable)
                if without_cards:
                    lines.append(
                        f"• {without_cards} 个缺少安全文件快照，请前往 Web 待确认队列处理"
                    )
                sections.append("\n".join(lines))
            else:
                sections.append(
                    f"⚠️ 待确认 {need_confirm} 个\n"
                    "本轮没有可安全操作的 Telegram 处理卡；"
                    "请前往 Web 待确认队列搜索、匹配或清理记录。"
                )

        skipped = safe_int(stats.get("skipped", 0), 0, minimum=0)
        reasons = []
        for value in stats.get("skip_reasons") or []:
            text = " ".join(str(value or "").split())
            if not text or text in reasons:
                continue
            reasons.append(text if len(text) <= 96 else f"{text[:95].rstrip()}…")
            if len(reasons) >= 3:
                break
        if skipped:
            lines = [f"⏭️ 跳过 {skipped} 个"]
            lines.extend(f"• {reason}" for reason in reasons)
            sections.append("\n".join(lines))

        cleaned = safe_int(stats.get("empty_dirs_cleaned", 0), 0, minimum=0)
        retained = (
            safe_int(stats.get("empty_dir_cleanup_not_empty", 0), 0, minimum=0)
            + safe_int(stats.get("empty_dir_cleanup_unavailable", 0), 0, minimum=0)
        )
        cleanup_reasons = []
        for value in stats.get("empty_dir_cleanup_reasons") or []:
            text = " ".join(str(value or "").split())
            if text and text not in cleanup_reasons:
                cleanup_reasons.append(text)
            if len(cleanup_reasons) >= 3:
                break
        if cleaned or retained or cleanup_reasons:
            summary = [f"清理 {cleaned} 个空目录"]
            if retained:
                summary.append(f"保留 {retained} 个非空或状态变化目录")
            lines = ["🧹 " + " · ".join(summary)]
            lines.extend(f"• {reason}" for reason in cleanup_reasons)
            sections.append("\n".join(lines))

        failed = safe_int(stats.get("failed", 0), 0, minimum=0)
        if failed:
            sections.append(f"❌ 失败 {failed} 个\n请在整理日志中查看失败文件和原因。")
        if (
            stats.get("scan_errors")
            or stats.get("scan_limited")
            or stats.get("scan_complete") is False
        ):
            sections.append("⚠️ 目录扫描未完整结束，本季集数与缺集统计不是最终结论。")
        return "\n\n".join(sections)

    @staticmethod
    def _notification_footer(stats: dict) -> str:
        lines: list[str] = []
        confirmations = [
            str(item).strip() for item in (stats.get("confirmations") or [])
            if str(item).strip()
        ]
        skip_reasons = [
            str(item).strip() for item in (stats.get("skip_reasons") or [])
            if str(item).strip()
        ]
        if confirmations:
            lines.append("待确认：" + "；".join(confirmations[:3]))
        if skip_reasons:
            lines.append("跳过原因：" + "；".join(skip_reasons[:3]))
        cleanup_reasons = [
            str(item).strip()
            for item in (stats.get("empty_dir_cleanup_reasons") or [])
            if str(item).strip()
        ]
        if cleanup_reasons:
            lines.append("目录清理：" + "；".join(cleanup_reasons[:3]))
        return "\n".join(lines)

    @staticmethod
    def _confirmation_summary(match: MatchResult) -> str:
        reason = str(match.error or "TMDB 匹配结果需人工确认").strip()
        if match.tmdb_id and match.title:
            year = f" ({match.year})" if match.year else ""
            return (
                f"{reason}；最佳候选：{match.title}{year} · "
                f"TMDB {match.tmdb_id} · {match.confidence:.0%}"
            )
        return reason

    @staticmethod
    def _cacheable_directory_match(
        match: MatchResult | None,
        *,
        automatic_threshold: float = 0.9,
        automatic_preset: str = "balanced",
    ) -> bool:
        if match is None or bool(getattr(match, "need_confirm", False)):
            return False
        if not bool(getattr(match, "directory_identity_cache_eligible", False)):
            return False
        if bool(getattr(match, "locked", False)):
            return False
        if getattr(match, "regex_rule_id", None) is not None:
            return False
        matched_by = str(getattr(match, "matched_by", "") or "")
        if matched_by in {"lock", "regex_rule", "tmdb_id"}:
            return False
        if matched_by.endswith("_hint") or matched_by.startswith("ai_"):
            return False
        if str(getattr(match, "provider", "") or "").strip().lower() == "metatube":
            return False
        # 低于全局阈值的强证据绑定到单个源文件及其季集位置，不能跨文件复用。
        # 即使运行时配置已经变化或 proof 已过期，只要存在该类元数据就按
        # 不可缓存处理，防止旧证明从“单文件例外”升级成目录级身份。
        metadata = dict(getattr(match, "metadata", None) or {})
        if isinstance(metadata.get("verified_automatic_identity_proof"), dict):
            return False
        if verified_automatic_identity_proof(
            match,
            global_threshold=automatic_threshold,
            preset=automatic_preset,
        ) is not None:
            return False
        return bool(
            str(getattr(match, "tmdb_id", "") or "").strip()
            or list(getattr(match, "candidates", None) or [])
        )

    @classmethod
    def _trusted_directory_tv_identity(
        cls,
        match: MatchResult | None,
        *,
        threshold: float = 0.9,
        automatic_preset: str = "balanced",
    ) -> str:
        """返回可在同一物理目录内复用的严格 TV/TMDB 身份。"""
        if match is None or not cls._cacheable_directory_match(
            match,
            automatic_threshold=threshold,
            automatic_preset=automatic_preset,
        ):
            return ""
        if str(getattr(match, "provider", "") or "").strip().lower() != "tmdb":
            return ""
        if str(getattr(match, "media_type", "") or "").strip().lower() != "tv":
            return ""
        if str(getattr(match, "status", "") or "").strip().lower() != "matched":
            return ""
        if float(getattr(match, "confidence", 0.0) or 0.0) < float(threshold):
            return ""
        tmdb_id = str(getattr(match, "tmdb_id", "") or "").strip()
        external_id = str(getattr(match, "external_id", "") or "").strip()
        if not tmdb_id or (external_id and external_id != tmdb_id):
            return ""
        return tmdb_id

    @staticmethod
    def _special_match_can_bind_identity(
        match: MatchResult | None, donor_tmdb_id: str
    ) -> bool:
        """仅接受已独立命中同一作品、因标题残片转人工的特殊内容。"""
        if match is None or not bool(getattr(match, "need_confirm", False)):
            return False
        if str(getattr(match, "provider", "") or "").strip().lower() != "tmdb":
            return False
        if str(getattr(match, "media_type", "") or "").strip().lower() != "tv":
            return False
        if str(getattr(match, "status", "") or "").strip().lower() != "low_confidence":
            return False
        if float(getattr(match, "confidence", 0.0) or 0.0) < 0.82:
            return False
        tmdb_id = str(getattr(match, "tmdb_id", "") or "").strip()
        external_id = str(getattr(match, "external_id", "") or "").strip()
        if tmdb_id != donor_tmdb_id or (external_id and external_id != donor_tmdb_id):
            return False
        return "候选仅命中部分标题" in str(getattr(match, "error", "") or "")

    @staticmethod
    def _physical_source_root(item: _ScannedVideo) -> str:
        """返回 source 下用于隔离作品身份的第一层物理目录。"""
        relative_parts = [
            part.strip()
            for part in re.split(r"[\\/]+", str(item.relative_dir or ""))
            if part.strip()
        ]
        if relative_parts and not is_special_directory_name(relative_parts[0]):
            return relative_parts[0].casefold()
        return "__root__"

    @staticmethod
    def _identity_only_directory_match(match: MatchResult) -> MatchResult:
        """复制目录身份并清除只属于原始文件的季集/证明状态。"""
        cloned = copy.deepcopy(match)
        cloned.need_confirm = False
        cloned.status = "matched"
        cloned.error = ""
        cloned.season_override = None
        cloned.effective_season = None
        cloned.effective_episode = None
        cloned.recognition_filename = ""
        cloned.recognition_parent_path = ""
        cloned.preprocess_rules = []
        cloned.preprocess_evaluated = False
        metadata = dict(getattr(cloned, "metadata", None) or {})
        for key in (
            "episode_mapping",
            "final_position_validation",
            "position_validation",
            "source_position",
            "target_position",
            "target_season_year_evidence",
            "verified_automatic_identity_proof",
            "verified_automatic_identity_proof_accepted",
            _DIRECTORY_PACKAGE_IDENTITY_ACCEPTED_KEY,
            _DIRECTORY_IDENTITY_ATTESTATION_ACCEPTED_KEY,
        ):
            metadata.pop(key, None)
        cloned.metadata = metadata
        return cloned

    @staticmethod
    def _identity_neutral_work_match(match: MatchResult) -> MatchResult:
        """复制精确搜索结果，但移除只能绑定首个识别位置的证明。

        工作缓存只减少相同查询的 TMDB 搜索次数，不能把 E01 的位置证明传播
        到 E02/E03。目录内身份复用由独立 attestation 完成，并且每个文件仍需
        重新解析位置、执行映射并通过最终 TMDB 季集门禁。
        """
        cloned = copy.deepcopy(match)
        cloned.season_override = None
        cloned.effective_season = None
        cloned.effective_episode = None
        metadata = dict(getattr(cloned, "metadata", None) or {})
        for key in (
            "episode_mapping",
            "final_position_validation",
            "position_validation",
            "source_position",
            "target_position",
            "target_season_year_evidence",
            "verified_automatic_identity_proof",
            "verified_automatic_identity_proof_accepted",
            _DIRECTORY_PACKAGE_IDENTITY_ACCEPTED_KEY,
            _DIRECTORY_IDENTITY_ATTESTATION_ACCEPTED_KEY,
        ):
            metadata.pop(key, None)
        cloned.metadata = metadata
        return cloned

    @staticmethod
    def _accepted_directory_identity_attestation(
        match: MatchResult | None,
    ) -> dict[str, object] | None:
        """从已逐文件验真的自动证明中提取仅限本次目录的作品身份。"""
        if match is None:
            return None
        metadata = getattr(match, "metadata", None)
        accepted = metadata.get("verified_automatic_identity_proof_accepted") \
            if isinstance(metadata, dict) else None
        if not isinstance(accepted, dict):
            return None
        validation = accepted.get("position_validation")
        tmdb_id = str(getattr(match, "tmdb_id", "") or "").strip()
        external_id = str(getattr(match, "external_id", "") or "").strip()
        if (
            not tmdb_id
            or Organizer._match_provider(match) != "tmdb"
            or str(getattr(match, "media_type", "") or "").strip().lower() != "tv"
            or str(accepted.get("tmdb_id") or "").strip() != tmdb_id
            or (external_id and external_id != tmdb_id)
            or not isinstance(validation, dict)
            or not validation.get("required")
            or not validation.get("passed")
            or str(validation.get("reason") or "") != "episode_verified"
        ):
            return None
        return {
            "version": _DIRECTORY_IDENTITY_ATTESTATION_VERSION,
            "kind": "same_scan_tmdb_tv_identity",
            "provider": "tmdb",
            "tmdb_id": tmdb_id,
            "media_type": "tv",
        }

    @staticmethod
    def _directory_title_anchor_score(anchor: str, candidates: list[str]) -> float:
        """返回目录/文件标题对候选标题的保守相似度。

        短英文标题（如 ``Boruto``）常只是完整原名的前缀；这种至少 6 个
        字符的边界内包含关系可作为强锚点。中文译名则使用字符序列相似度，
        但只作为第二锚点，不能单独触发自动整理。
        """
        normalized_anchor = _normalize_media_identity(anchor)
        if not normalized_anchor:
            return 0.0
        best = 0.0
        for candidate in candidates:
            normalized_candidate = _normalize_media_identity(candidate)
            if not normalized_candidate:
                continue
            if normalized_anchor == normalized_candidate:
                return 1.0
            shorter, longer = sorted(
                (normalized_anchor, normalized_candidate), key=len
            )
            if len(shorter) >= 6 and shorter in longer:
                best = max(best, 0.82)
            best = max(
                best,
                SequenceMatcher(
                    None, normalized_anchor, normalized_candidate
                ).ratio(),
            )
        return float(round(best, 3))

    @classmethod
    def _build_directory_package_identity_proof(
        cls,
        match: MatchResult | None,
        evidence: DirectoryEpisodeEvidence | None,
        member_count: int,
        *,
        automatic_threshold: float = 0.9,
        automatic_preset: str = "balanced",
    ) -> dict[str, object] | None:
        """为完整连续剧集包构建低于全局阈值的目录身份凭证。

        该凭证只证明“整包属于哪个 TMDB 剧集”，绝不证明任一文件的季集
        位置。每个文件仍必须经过 episode mapping 与最终 TMDB 位置校验。
        """
        if match is None or evidence is None:
            return None
        if (
            not evidence.contiguous
            or evidence.range_start != 1
            or evidence.episode_count < _DIRECTORY_PACKAGE_IDENTITY_MIN_EPISODES
            or int(member_count or 0) != evidence.episode_count
            or evidence.range_end - evidence.range_start + 1
            != evidence.episode_count
        ):
            return None
        if (
            bool(getattr(match, "need_confirm", False))
            or bool(getattr(match, "locked", False))
            or getattr(match, "regex_rule_id", None) is not None
            or str(getattr(match, "status", "") or "").strip().lower()
            != "matched"
            or str(getattr(match, "matched_by", "") or "").strip().lower()
            != "search"
            or str(getattr(match, "provider", "") or "").strip().lower()
            != "tmdb"
            or str(getattr(match, "media_type", "") or "").strip().lower()
            != "tv"
        ):
            return None
        confidence = float(getattr(match, "confidence", 0.0) or 0.0)
        if not (
            _DIRECTORY_PACKAGE_IDENTITY_MIN_CONFIDENCE
            <= confidence
            < min(float(automatic_threshold), 0.9)
        ):
            return None
        tmdb_id = str(getattr(match, "tmdb_id", "") or "").strip()
        external_id = str(getattr(match, "external_id", "") or "").strip()
        if not tmdb_id or external_id != tmdb_id:
            return None
        if list(getattr(match, "rejected_constraints", None) or []):
            return None
        if dict(getattr(match, "ai_diagnostic", None) or {}):
            return None
        threshold_decision = getattr(match, "threshold_decision", None)
        if not isinstance(threshold_decision, dict) or not bool(
            threshold_decision.get("passed")
        ):
            return None

        candidates = list(getattr(match, "candidates", None) or [])
        if len(candidates) != 1:
            return None
        candidate = candidates[0]
        candidate_tmdb_id = str(
            getattr(candidate, "tmdb_id", "")
            or getattr(candidate, "external_id", "")
            or ""
        ).strip()
        if (
            candidate_tmdb_id != tmdb_id
            or str(getattr(candidate, "provider", "tmdb") or "tmdb")
            .strip().lower() != "tmdb"
            or str(getattr(candidate, "media_type", "") or "").strip().lower()
            != "tv"
        ):
            return None
        breakdown = getattr(candidate, "score_breakdown", None)
        if breakdown is None or list(
            getattr(breakdown, "rejected_constraints", None) or []
        ):
            return None
        breakdown_score = max(
            float(getattr(breakdown, "title_score", 0.0) or 0.0),
            float(getattr(breakdown, "original_title_score", 0.0) or 0.0),
            float(getattr(breakdown, "alias_score", 0.0) or 0.0),
        )
        if breakdown_score < _DIRECTORY_PACKAGE_IDENTITY_MIN_BREAKDOWN_SCORE:
            return None

        context = getattr(match, "context", None)
        if context is None or str(
            getattr(context, "media_type", "") or ""
        ).strip().lower() != "tv":
            return None
        filename_anchor = str(
            getattr(context, "filename_title", "")
            or getattr(context, "normalized_title", "")
            or ""
        ).strip()
        folder_anchor = str(getattr(context, "folder_title", "") or "").strip()
        if (
            not filename_anchor
            or not folder_anchor
            or _normalize_media_identity(filename_anchor)
            == _normalize_media_identity(folder_anchor)
        ):
            return None
        candidate_titles = list(dict.fromkeys(
            str(value).strip()
            for value in (
                getattr(candidate, "title", ""),
                getattr(candidate, "original_title", ""),
                *(getattr(candidate, "aliases", None) or []),
                getattr(breakdown, "matched_title", ""),
                getattr(match, "title", ""),
            )
            if str(value).strip()
        ))
        filename_score = cls._directory_title_anchor_score(
            filename_anchor, candidate_titles
        )
        folder_score = cls._directory_title_anchor_score(
            folder_anchor, candidate_titles
        )
        if (
            filename_score < _DIRECTORY_PACKAGE_IDENTITY_MIN_FILE_ANCHOR_SCORE
            or folder_score < _DIRECTORY_PACKAGE_IDENTITY_MIN_FOLDER_ANCHOR_SCORE
        ):
            return None
        return {
            "version": _DIRECTORY_PACKAGE_IDENTITY_PROOF_VERSION,
            "tmdb_id": tmdb_id,
            "external_id": external_id,
            "confidence": round(confidence, 3),
            "automatic_match_preset": normalize_automatic_match_preset(
                automatic_preset
            ),
            "global_threshold": round(float(automatic_threshold), 3),
            "candidate_count": 1,
            "breakdown_score": round(breakdown_score, 3),
            "filename_anchor": filename_anchor,
            "filename_anchor_score": filename_score,
            "folder_anchor": folder_anchor,
            "folder_anchor_score": folder_score,
            "directory_key": evidence.directory_key,
            "source_season": evidence.source_season,
            "range_start": evidence.range_start,
            "range_end": evidence.range_end,
            "episode_count": evidence.episode_count,
            "member_count": int(member_count),
        }

    @classmethod
    def _directory_package_identity_proof(
        cls,
        match: MatchResult | None,
        evidence: DirectoryEpisodeEvidence | None,
        member_count: int,
        *,
        automatic_threshold: float = 0.9,
        automatic_preset: str = "balanced",
    ) -> dict[str, object] | None:
        """复核 match 内已有的目录身份凭证是否仍匹配当前连续包。

        不只比对范围与 ID，而是重新执行构建时的全部强度条件。这样即使
        metadata 来自陈旧缓存或外部输入，也不能绕过单候选、双标题锚点、
        置信度区间、TMDB provider 与无 AI/约束拒绝等门禁。
        """
        if match is None or evidence is None:
            return None
        metadata = getattr(match, "metadata", None)
        proof = metadata.get(_DIRECTORY_PACKAGE_IDENTITY_PROOF_KEY) \
            if isinstance(metadata, dict) else None
        if not isinstance(proof, dict):
            return None
        rebuilt = cls._build_directory_package_identity_proof(
            match,
            evidence,
            member_count,
            automatic_threshold=automatic_threshold,
            automatic_preset=automatic_preset,
        )
        if rebuilt is None or proof != rebuilt:
            return None
        return proof

    @staticmethod
    def _accepted_directory_package_identity_proof(
        match: MatchResult | None,
    ) -> dict[str, object] | None:
        if match is None:
            return None
        metadata = getattr(match, "metadata", None)
        if not isinstance(metadata, dict):
            return None
        proof = metadata.get(_DIRECTORY_PACKAGE_IDENTITY_PROOF_KEY)
        accepted = metadata.get(_DIRECTORY_PACKAGE_IDENTITY_ACCEPTED_KEY)
        if not isinstance(proof, dict) or not isinstance(accepted, dict):
            return None
        tmdb_id = str(getattr(match, "tmdb_id", "") or "").strip()
        if (
            not tmdb_id
            or str(proof.get("tmdb_id") or "").strip() != tmdb_id
            or str(accepted.get("tmdb_id") or "").strip() != tmdb_id
        ):
            return None
        validation = accepted.get("position_validation")
        if not isinstance(validation, dict) or not (
            validation.get("required")
            and validation.get("passed")
            and str(validation.get("reason") or "") == "episode_verified"
        ):
            return None
        return proof

    def _directory_identity_cache_key(
        self,
        item: _ScannedVideo,
        rules: OrganizeRules,
        *,
        parent_path_override: str | None = None,
    ) -> tuple[str, str, str, str] | None:
        """为同一物理目录中的连续剧文件生成保守的身份复用键。"""
        if not isinstance(self.scraper, TMDBScraper) or item.special or rules.nsfw_enabled:
            return None
        filename = str(item.file.name or "")
        parent_path = str(
            item.relative_dir if parent_path_override is None else parent_path_override
        )
        if _has_explicit_tmdb_marker(f"{filename} {parent_path}"):
            return None
        try:
            processed = self.scraper.prepare_recognition(filename, parent_path)
            context = extract_recognition_context(processed.filename, processed.parent_path)
        except Exception:
            return None
        title = _normalize_media_identity(context.normalized_title)
        if (
            context.media_type != "tv"
            or context.episode is None
            or not title
            or title.isdigit()
            or len(title) < 3
        ):
            return None
        # 显式人工锁和强制规则可能按单个文件生效，不能被目录缓存越过。
        lock_getter = getattr(self.scraper, "_get_lock", None)
        if callable(lock_getter):
            try:
                if lock_getter(filename, parent_path, media_type_hint="tv"):
                    return None
            except Exception:
                return None
        try:
            from app.modules.tmdb_regex_rules import find_tmdb_regex_match
            if find_tmdb_regex_match(filename, parent_path, "tv"):
                return None
        except Exception:
            return None
        return (
            parent_path.casefold(),
            title,
            str(context.filename_year or context.folder_year or ""),
            "tv",
        )

    def _recognition_work_cache_key(
        self,
        *,
        recognition_name: str,
        parent_path: str,
        media_type_hint: str,
        rules: OrganizeRules,
        automatic: bool,
        trusted_match_override: MatchResult | None,
    ) -> tuple[str, str, str, bool, str] | None:
        """返回一次整理任务内可安全复用的精确识别工作键。

        该缓存只复用完全相同的识别输入及其原始 MatchResult，不赋予目录级
        信任，也不绕过后续逐文件季集映射、TMDB 位置验证或人工确认门禁。
        """
        if (
            type(self.scraper) is not TMDBScraper
            or rules.nsfw_enabled
            or trusted_match_override is not None
        ):
            return None
        exact_name = str(recognition_name or "").strip()
        exact_parent = str(parent_path or "").strip()
        if not exact_name or _has_explicit_tmdb_marker(f"{exact_name} {exact_parent}"):
            return None
        return (
            exact_parent.casefold(),
            exact_name,
            str(media_type_hint or "").strip().lower(),
            bool(automatic),
            str(rules.automatic_match_preset or "balanced").strip().lower(),
        )

    def _special_position_overrides(
        self, candidates: list[_ScannedVideo]
    ) -> dict[str, tuple[int, int]]:
        groups: dict[tuple[str, str], list[_ScannedVideo]] = {}
        for item in candidates:
            if not item.special:
                continue
            physical_root = self._physical_source_root(item)
            owner_hint = title_hint_from_path(
                item.recognition_parent_path, item.recognition_parent_path
            )
            # source 根目录可能混放多个作品。小数集/OVA 文件若自身携带
            # 作品标题，必须优先以文件名标题隔离 S00 编号空间；否则两个
            # 独立作品的 ``1.5`` 会被错误连续编号为 E01/E02。
            filename_owner_hint = _special_filename_identity_hint(item.file.name)
            if physical_root == "__root__" and filename_owner_hint:
                owner_hint = filename_owner_hint
            # 同一作品的不同发布目录常只在 ``[01-10]`` / ``[01-12]``、
            # GB/BIG5 等批次标签上不同；先移除集数范围并复用标题清洗，
            # 让分散在这些兄弟目录中的 NCOP/NCED 共用一个 S00 编号空间。
            owner_identity = re.sub(
                r"[\[【(（]\s*(?:e?p?\s*)?\d{1,3}\s*[-~–—]\s*"
                r"\d{1,3}\s*[\]】)）]",
                " ",
                str(owner_hint or ""),
                flags=re.IGNORECASE,
            )
            clean_title = getattr(self.scraper, "clean_title", None)
            if callable(clean_title):
                try:
                    owner_identity = str(clean_title(owner_identity) or owner_identity)
                except Exception:
                    pass
            # 用 source 下第一层物理树隔离互不相关的作品；若第一层本身就是
            # Extra/SPs 等特殊目录，则它仍归属于当前 source 根作品。这样既
            # 避免相同清洗标题跨树串号，又保留同作品 Extra/SPs 的连续编号。
            key = (
                physical_root,
                _normalize_media_identity(
                    owner_identity or item.recognition_parent_path or "/"
                ),
            )
            groups.setdefault(key, []).append(item)

        result: dict[str, tuple[int, int]] = {}
        for items in groups.values():
            ordered = sorted(
                items,
                key=lambda current: (
                    current.relative_dir.casefold(),
                    current.file.name.casefold(),
                    current.file.file_id,
                ),
            )
            # 小数集只在它们原有的排序槽位内按数值重排，既保证
            # 1.5/4.5/7.5 的稳定顺序，也不改变 NCOP/NCED 等既有行为。
            fractional_slots = [
                index
                for index, item in enumerate(ordered)
                if fractional_episode_position(item.file.name) is not None
            ]
            fractional_items = sorted(
                (ordered[index] for index in fractional_slots),
                key=lambda item: (
                    fractional_episode_position(item.file.name)
                    or (-1, Decimal(0)),
                    item.file.name.casefold(),
                    item.file.file_id,
                ),
            )
            for index, item in zip(fractional_slots, fractional_items):
                ordered[index] = item

            used: set[int] = set()
            reserved_ids: set[str] = set()
            # S00E## 与 SxxE00 是用户/发布名给出的绝对位置，必须先占位；
            # NCOP2/NCED1 仍按旧有顺序分配，避免把裸 NCOP 挤到 E02。
            for item in ordered:
                desired = fixed_special_media_position(item.file.name)
                if desired is None or desired <= 0 or desired in used:
                    continue
                used.add(desired)
                reserved_ids.add(item.file.file_id)
                result[item.file.file_id] = (0, desired)

            next_episode = 1
            for item in ordered:
                if item.file.file_id in reserved_ids:
                    continue
                desired: int | None = None
                if fractional_episode_position(item.file.name) is None:
                    desired = special_media_position(item.file.name)
                    if desired is None:
                        release_parse = self.scraper.parse_media(item.file.name)
                        desired = release_parse.source_episode
                        try:
                            desired = int(desired) if desired is not None else None
                        except (TypeError, ValueError):
                            desired = None
                if desired is None or desired <= 0 or desired in used:
                    while next_episode in used:
                        next_episode += 1
                    desired = next_episode
                used.add(desired)
                next_episode = max(next_episode, desired + 1)
                result[item.file.file_id] = (0, desired)
        return result

    def _resolve_plan_match(
        self,
        file: GuangYaFile,
        rules: OrganizeRules,
        *,
        match_name: str,
        parent_path: str,
        recognition_media_type_hint: str,
        match_override: MatchResult | None,
        recognition_work_cache: dict[
            tuple[str, str, str, bool, str], MatchResult
        ] | None,
        recognition_work_cache_key: tuple[str, str, str, bool, str] | None,
        parsed_override: tuple[int | None, int | None] | None = None,
    ) -> MatchResult | None:
        """解析单文件身份，并只缓存与具体季集位置无关的确定性结果。"""
        match = copy.deepcopy(match_override) if match_override is not None else None
        if (
            match is None
            and recognition_work_cache is not None
            and recognition_work_cache_key is not None
        ):
            cached_work = recognition_work_cache.get(recognition_work_cache_key)
            if cached_work is not None:
                match = copy.deepcopy(cached_work)
        if match is not None and isinstance(self.scraper, TMDBScraper):
            try:
                processed = self.scraper.prepare_recognition(match_name, parent_path)
                match = self.scraper._attach_preprocess(match, processed)
            except Exception:
                match = None
        if (
            match is not None
            and rules.nsfw_exclusive
            and self._match_provider(match) not in {"metatube", "clean_title"}
        ):
            # 专用来源不能复用历史 TMDB 缓存、强制映射或其它普通影视结果。
            match = None
        # 小数集号只有在扫描阶段已生成确定性的 Season 00 位置，或用户
        # 明确指定了目标集号时才允许继续；无法建立稳定映射时仍失败关闭。
        fractional_position = has_fractional_episode_position(file.name)
        manual_fractional_override = bool(
            getattr(self.scraper, "manual_position_confirmed", False)
            and getattr(self.scraper, "episode_override", None) is not None
        )
        if fractional_position and parsed_override is None and not manual_fractional_override:
            match = MatchResult(
                media_type="tv",
                need_confirm=True,
                status="low_confidence",
                matched_by="fractional_episode",
                threshold=1.0,
                error="检测到小数集号但无法建立稳定的特别篇顺序，请手动指定目标集数",
            )
        if match is None and type(self.scraper) is TMDBScraper:
            recognizer = self._nsfw_recognizer(rules)
            if recognizer is not None:
                match = recognizer.match(match_name, parent_path)
        if (
            match is None
            and rules.nsfw_exclusive
            and type(self.scraper) is TMDBScraper
        ):
            return self._nsfw_unresolved_match()
        if match is None:
            if isinstance(self.scraper, TMDBScraper):
                match = self.scraper.match(
                    match_name,
                    parent_path,
                    media_type_hint=recognition_media_type_hint,
                )
            elif bool(getattr(self.scraper, "supports_parent_path", False)):
                if recognition_media_type_hint:
                    try:
                        match = self.scraper.match(
                            match_name,
                            parent_path,
                            media_type_hint=recognition_media_type_hint,
                        )
                    except TypeError as exc:
                        # 兼容只声明 parent_path、尚未接受类型提示的旧扩展识别器。
                        if "media_type_hint" not in str(exc):
                            raise
                        match = self.scraper.match(match_name, parent_path)
                else:
                    match = self.scraper.match(match_name, parent_path)
            else:
                match = self.scraper.match(match_name)
            if (
                match is not None
                and rules.nsfw_exclusive
                and self._match_provider(match) not in {"metatube", "clean_title"}
            ):
                return self._nsfw_unresolved_match()
            if (
                recognition_work_cache is not None
                and recognition_work_cache_key is not None
                and match is not None
                and str(getattr(match, "matched_by", "") or "").strip().lower()
                == "search"
                and str(getattr(match, "status", "") or "").strip().lower()
                == "matched"
                and not bool(getattr(match, "need_confirm", False))
            ):
                # 只缓存普通 TMDB 搜索的原始结果。Bangumi/Tavily/AI 等外部
                # 提示属于慢路径证据，必须逐文件重新验证，不能因为识别输入
                # 相同就扩散到整组。保存动作仍发生在逐文件位置校验和确认
                # 状态调整前，避免把首集的 SxxExx 或冲突结论传播给后续文件。
                recognition_work_cache[recognition_work_cache_key] = (
                    self._identity_neutral_work_match(match)
                )
        return match

    @staticmethod
    def _metadata_episode_mapping(
        match: MatchResult,
        *,
        source_season: int | None,
        source_episode: int | None,
        parsed_season: int | None,
        parsed_episode: int | None,
    ) -> EpisodeMappingPlan | None:
        """仅恢复与当前源位置和目标位置完全一致的高置信映射。"""
        from app.notifier import safe_int

        mapping_payload = dict(
            (getattr(match, "metadata", None) or {}).get("episode_mapping") or {}
        )
        if not mapping_payload.get("changed"):
            return None
        mapping_source_season = safe_int(
            mapping_payload.get("source_season"), None, minimum=0
        )
        mapping_source_episode = safe_int(
            mapping_payload.get("source_episode"), None, minimum=0
        )
        mapping_target_season = safe_int(
            mapping_payload.get("target_season"), None, minimum=0
        )
        mapping_target_episode = safe_int(
            mapping_payload.get("target_episode"), None, minimum=0
        )
        mapping_mode = str(mapping_payload.get("mode") or "").strip().lower()
        raw_source_season = mapping_payload.get("source_season")
        source_season_valid = (
            raw_source_season in (None, "")
            or mapping_source_season is not None
        )
        try:
            mapping_confidence = float(
                mapping_payload.get("confidence")
                if mapping_payload.get("confidence") is not None
                else 1.0
            )
        except (TypeError, ValueError):
            mapping_confidence = 0.0
        if not (
            source_season_valid
            and mapping_mode in {"absolute", "season_continuous"}
            and mapping_source_season == source_season
            and mapping_source_episode == source_episode
            and mapping_target_season == parsed_season
            and mapping_target_episode == parsed_episode
            and mapping_source_episode is not None
            and mapping_target_season not in (None, 0)
            and mapping_target_episode is not None
            and mapping_confidence >= 0.9
        ):
            return None
        return EpisodeMappingPlan(
            mapping_source_season,
            mapping_source_episode,
            mapping_target_season,
            mapping_target_episode,
            mode=mapping_mode,
            reason=str(mapping_payload.get("reason") or "identity"),
            confidence=mapping_confidence,
            range_start=safe_int(
                mapping_payload.get("range_start"), None, minimum=1
            ),
            range_end=safe_int(
                mapping_payload.get("range_end"), None, minimum=1
            ),
        )

    def _populate_move_plan(
        self,
        plan: OrganizePlan,
        file: GuangYaFile,
        rules: OrganizeRules,
        match: MatchResult,
        detail: dict,
        parsed: dict,
        *,
        parsed_season: int | None,
        automatic: bool,
        media_probe_cache_only: bool,
        media_probe_cached_payload: str,
        media_probe_cache_prefetched: bool,
    ) -> None:
        """在所有身份和季集安全门通过后组装最终移动计划。"""
        from app.notifier import safe_int

        main, region, year = self.classify(match, rules)
        plan.main_category, plan.region, plan.year = main, region, year
        plan.backdrop_path = str(
            detail.get("backdrop_path") or detail.get("backdrop_url") or ""
        )
        plan.poster_path = str(
            detail.get("poster_path") or detail.get("poster_url") or ""
        )
        if match.media_type == "tv" and parsed_season is not None:
            for season in detail.get("seasons") or []:
                if not isinstance(season, dict):
                    continue
                season_number = safe_int(
                    season.get("season_number"), None, minimum=0
                )
                if season_number == parsed_season:
                    plan.season_total = safe_int(
                        season.get("episode_count"), 0, minimum=0
                    )
                    break
        media_profile = None
        if rules.media_info_enabled and rules.media_probe_enabled:
            from app.modules.media_probe import probe_media_profile
            media_profile = probe_media_profile(
                file,
                self.client,
                enabled=True,
                timeout=(
                    min(rules.media_probe_timeout, 30)
                    if automatic
                    else rules.media_probe_timeout
                ),
                # 在线探测由 _build_plans 在身份/季集复核完成后批量执行；
                # 这里仅消费预取缓存，确保冲突预演前名称仍可稳定重算。
                cache_only=True,
                prefetched_payload=media_probe_cached_payload,
                cache_prefetched=media_probe_cache_prefetched,
                budget=self._probe_budget,
            )
        plan.media_probe_complete = media_profile is not None
        self._apply_media_profile_to_move_plan(
            plan, file, rules, match, parsed, media_profile,
        )

        # 目标路径：主类[/地区][/年份]/媒体目录[/Season N]
        parts = [main]
        if rules.region_split and self._match_provider(match) not in {"metatube", "clean_title"}:
            parts.append(region)
        if rules.year_split and year:
            parts.append(year)
        media_dir = self.build_media_dir(match, rules)
        plan.media_root_path = "/".join([*parts, media_dir])
        season_dir = self.build_season_dir(match, parsed)
        plan.target_path = "/".join(
            [*parts, media_dir, *([season_dir] if season_dir else [])]
        )
        scope = {
            item.strip()
            for item in str(rules.naming_scope or "guangya").split(",")
            if item.strip()
        }
        if {"both", "guangya"} & scope:
            directory_template = (
                rules.show_dir_template
                if match.media_type == "tv"
                else rules.movie_dir_template
            )
            plan.identity_guard_required = not template_has_media_identity(
                directory_template,
                tmdb_id=match.tmdb_id,
                identity_id=self._match_identity_key(match),
            )

    def _apply_media_profile_to_move_plan(
        self,
        plan: OrganizePlan,
        file: GuangYaFile,
        rules: OrganizeRules,
        match: MatchResult,
        parsed: dict,
        media_profile,
    ) -> None:
        """按字段合并探测与发布名证据，再重算名称和版本身份。"""
        from app.modules.media_probe import infer_media_profile, merge_media_profiles

        source_hint = "/".join(
            part for part in (
                str(plan.original_path or ""), str(plan.original_name or ""),
                str(file.name or ""),
            ) if part
        )
        effective_profile = merge_media_profiles(
            media_profile, infer_media_profile(source_hint),
        )
        media_info_override = effective_profile.render()
        plan.media_profile = effective_profile
        plan.season = parsed.get("season")
        plan.episode = parsed.get("episode")
        try:
            plan.multipart_index = (
                int(parsed.get("part")) if parsed.get("part") is not None else None
            )
        except (TypeError, ValueError):
            plan.multipart_index = None
        plan.variant = classify_variant(file.name, effective_profile)
        variant_tags = plan.variant.filename_tags(rules)
        plan.variant_label = " / ".join(variant_tags) if variant_tags else "未识别版本"
        plan.variant_suffix = ".".join(variant_tags)
        plan.base_name = self.build_new_name(
            match, file, parsed, rules,
            include_media_info=False,
            include_variant_tags=False,
        )
        plan.new_name = self.build_new_name(
            match,
            file,
            parsed,
            rules,
            media_info_override=media_info_override,
            media_variant_override=effective_profile,
        )

    def _apply_media_source_consensus(
        self,
        plans: list[OrganizePlan],
        source_files_by_id: dict[str, GuangYaFile],
        *,
        rules: OrganizeRules,
        stats: dict,
    ) -> None:
        """同父目录、同媒体、同季的来源强证据一致时补齐未知集。"""
        from app.modules.media_probe import MediaProfile, infer_media_source

        groups: dict[tuple[str, str, int], list[OrganizePlan]] = {}
        for plan in plans:
            if (
                plan.action != "move"
                or plan.match is None
                or plan.match.media_type != "tv"
                or plan.season is None
            ):
                continue
            identity = self._match_identity_key(plan.match)
            if not identity:
                continue
            key = (str(plan.original_parent_id or ""), identity, int(plan.season))
            groups.setdefault(key, []).append(plan)

        applied_groups = 0
        applied_items = 0
        for group_plans in groups.values():
            explicit_sources = [
                infer_media_source(
                    "/".join(part for part in (plan.original_path, plan.original_name) if part)
                )
                for plan in group_plans
            ]
            explicit_sources = [source for source in explicit_sources if source]
            if len(explicit_sources) < 2 or len(set(explicit_sources)) != 1:
                continue
            consensus = explicit_sources[0]
            changed = 0
            for plan in group_plans:
                file = source_files_by_id.get(str(plan.file_id or ""))
                profile = plan.media_profile
                if file is None or (profile is not None and getattr(profile, "source", "")):
                    continue
                effective = replace(
                    profile if profile is not None else MediaProfile(),
                    source=consensus,
                )
                self._apply_media_profile_to_move_plan(
                    plan, file, rules, plan.match,
                    {"season": plan.season, "episode": plan.episode}, effective,
                )
                changed += 1
            if changed:
                applied_groups += 1
                applied_items += changed
        stats["media_source_consensus_groups"] = applied_groups
        stats["media_source_consensus_items"] = applied_items

    def _probe_move_plan_profiles(
        self,
        plans: list[OrganizePlan],
        source_files_by_id: dict[str, GuangYaFile],
        source_probe_cache: dict[tuple[str, str, int], str],
        *,
        cache_prefetched: bool,
        rules: OrganizeRules,
        automatic: bool,
        cache_only: bool,
        stats: dict,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """对已经通过身份安全门的移动计划执行有界并发在线探测。"""
        stats["media_probe_online_candidates"] = 0
        stats["media_probe_online_profiles"] = 0
        stats["media_probe_elapsed_seconds"] = 0.0

        def mark_pending() -> None:
            for plan in plans:
                if plan.action == "move" and plan.match is not None:
                    plan.media_probe_pending = not plan.media_probe_complete

        if not rules.media_info_enabled or not rules.media_probe_enabled:
            # 用户显式关闭媒体详情或在线探测时必须保持关闭语义，不能在
            # 整理完成后又由后台 worker 悄悄发起 ffprobe。
            for plan in plans:
                plan.media_probe_pending = False
            return
        if cache_only:
            mark_pending()
            return

        candidates: list[GuangYaFile] = []
        seen: set[str] = set()
        for plan in plans:
            file_id = str(plan.file_id or "")
            if plan.action != "move" or not file_id or file_id in seen:
                continue
            file = source_files_by_id.get(file_id)
            if file is None:
                continue
            seen.add(file_id)
            candidates.append(file)
        if not candidates:
            mark_pending()
            return

        from app.modules.media_probe import (
            probe_media_profiles_batch,
            resolve_media_probe_workers,
        )

        stats["media_probe_online_candidates"] = len(candidates)
        stats["media_probe_workers"] = min(
            resolve_media_probe_workers(), len(candidates)
        )
        started = time.monotonic()
        profiles = probe_media_profiles_batch(
            candidates,
            self.client,
            enabled=True,
            timeout=(
                min(rules.media_probe_timeout, 30)
                if automatic
                else rules.media_probe_timeout
            ),
            prefetched_payloads=source_probe_cache,
            cache_prefetched=cache_prefetched,
            budget=self._probe_budget,
            max_workers=stats["media_probe_workers"],
            cancel_event=cancel_event,
        )
        stats["media_probe_elapsed_seconds"] = round(
            time.monotonic() - started, 3
        )
        stats["media_probe_online_profiles"] = len(profiles)

        plans_by_file_id = {
            str(plan.file_id): plan
            for plan in plans
            if plan.action == "move" and plan.match is not None
        }
        for file_id, profile in profiles.items():
            plan = plans_by_file_id.get(str(file_id))
            file = source_files_by_id.get(str(file_id))
            if plan is None or file is None or plan.match is None:
                continue
            parsed = {"season": plan.season, "episode": plan.episode}
            plan.media_probe_complete = True
            self._apply_media_profile_to_move_plan(
                plan, file, rules, plan.match, parsed, profile,
            )
        mark_pending()

    def _plan_one(
        self,
        file: GuangYaFile,
        rel: str,
        rules: OrganizeRules,
        *,
        recognition_parent_path: str | None = None,
        parsed_override: tuple[int | None, int | None] | None = None,
        source_position_override: tuple[int | None, int | None] | None = None,
        directory_episode_evidence: DirectoryEpisodeEvidence | None = None,
        directory_episode_member_count: int = 0,
        recognition_name: str = "",
        recognition_media_type_hint: str = "",
        match_override: MatchResult | None = None,
        directory_identity_attestation: dict[str, object] | None = None,
        recognition_work_cache: dict[tuple[str, str, str, bool, str], MatchResult] | None = None,
        recognition_work_cache_key: tuple[str, str, str, bool, str] | None = None,
        media_probe_cache_only: bool = False,
        media_probe_cached_payload: str = "",
        media_probe_cache_prefetched: bool = False,
        automatic: bool = False,
    ) -> OrganizePlan:
        match_name = str(recognition_name or file.name)
        parent_path = rel if recognition_parent_path is None else recognition_parent_path
        manual_position_confirmed = bool(
            getattr(self.scraper, "manual_position_confirmed", False)
            and getattr(self.scraper, "episode_override", None) is not None
        )
        effective_parsed_override = None if manual_position_confirmed else parsed_override
        match = self._resolve_plan_match(
            file,
            rules,
            match_name=match_name,
            parent_path=parent_path,
            recognition_media_type_hint=recognition_media_type_hint,
            match_override=match_override,
            recognition_work_cache=recognition_work_cache,
            recognition_work_cache_key=recognition_work_cache_key,
            parsed_override=effective_parsed_override,
        )
        plan = OrganizePlan(
            file_id=file.file_id, original_name=file.name,
            original_path=rel, original_parent_id=file.parent_id,
            size=file.size, etag=file.etag, match=match,
        )
        # 显式 TMDB 标记是用户给出的强身份约束，而不是“提高搜索命中率”的
        # 普通提示。自动整理必须重新绑定返回结果：客户端/缓存即使返回了别的
        # ID，或把 SxxExx/特典错误识别为电影，也只能转人工确认，不能写入网盘。
        explicit_tmdb_id, explicit_tmdb_conflict = _resolve_explicit_tmdb_marker(
            match_name
        )
        if not explicit_tmdb_conflict and not explicit_tmdb_id:
            explicit_tmdb_id, explicit_tmdb_conflict = _resolve_explicit_tmdb_marker(
                parent_path, nearest_first=True,
            )
        if automatic and explicit_tmdb_conflict:
            marker_error = "同一路径层级包含多个不同 TMDB 标记，已阻止自动整理"
            match.need_confirm = True
            match.status = "low_confidence"
            match.error = marker_error
            rejected = getattr(match, "rejected_constraints", None)
            if isinstance(rejected, list) and "explicit_tmdb_marker_conflict" not in rejected:
                rejected.append("explicit_tmdb_marker_conflict")
            plan.action = "skip"
            plan.note = marker_error
            return plan
        if automatic and explicit_tmdb_id:
            recognized_tmdb_id = str(
                getattr(match, "tmdb_id", "") or ""
            ).strip()
            recognized_external_id = str(
                getattr(match, "external_id", "") or ""
            ).strip()
            recognized_ids = list(dict.fromkeys(
                value for value in (recognized_tmdb_id, recognized_external_id) if value
            ))
            recognition_context = extract_recognition_context(match_name, parent_path)
            expects_tv = bool(
                str(recognition_media_type_hint or "").strip().lower() == "tv"
                or recognition_context.media_type == "tv"
                or recognition_context.season is not None
                or recognition_context.episode is not None
                or parsed_override is not None
                or source_position_override is not None
            )
            marker_error = ""
            if (
                recognized_tmdb_id != explicit_tmdb_id
                or any(value != explicit_tmdb_id for value in recognized_ids)
            ):
                marker_error = (
                    f"显式 TMDB 标记 {explicit_tmdb_id} 与识别结果 "
                    f"{'/'.join(recognized_ids) or '为空'} 不一致，需人工确认"
                )
            elif expects_tv and str(getattr(match, "media_type", "") or "").lower() != "tv":
                marker_error = "显式 TMDB 标记所在文件包含剧集/特典位置，但识别结果不是剧集，需人工确认"
            if marker_error:
                match.need_confirm = True
                match.status = "low_confidence"
                match.error = marker_error
                rejected = getattr(match, "rejected_constraints", None)
                if isinstance(rejected, list) and "explicit_tmdb_marker_mismatch" not in rejected:
                    rejected.append("explicit_tmdb_marker_mismatch")
                plan.action = "skip"
                plan.note = marker_error
                return plan
        automatic_policy = automatic_match_policy(rules.automatic_match_preset)
        automatic_proof = (
            verified_automatic_identity_proof(
                match,
                global_threshold=automatic_policy.threshold,
                preset=automatic_policy.name,
            )
            if automatic
            else None
        )
        directory_package_proof = (
            self._directory_package_identity_proof(
                match,
                directory_episode_evidence,
                directory_episode_member_count,
                automatic_threshold=automatic_policy.threshold,
                automatic_preset=automatic_policy.name,
            )
            if automatic
            else None
        )
        if (
            automatic
            and directory_package_proof is None
            and automatic_proof is None
        ):
            directory_package_proof = self._build_directory_package_identity_proof(
                match,
                directory_episode_evidence,
                directory_episode_member_count,
                automatic_threshold=automatic_policy.threshold,
                automatic_preset=automatic_policy.name,
            )
            if directory_package_proof is not None:
                match.metadata = {
                    **dict(getattr(match, "metadata", None) or {}),
                    _DIRECTORY_PACKAGE_IDENTITY_PROOF_KEY: directory_package_proof,
                }
        if (
            match.need_confirm
            and automatic_proof is None
            and directory_package_proof is None
            and directory_identity_attestation is None
        ):
            plan.action = "skip"
            plan.note = match.error or "媒体匹配结果需人工确认"
            return plan
        if (
            automatic
            and automatic_match_requires_confirmation(
                match, threshold=automatic_policy.threshold
            )
            and automatic_proof is None
            and directory_package_proof is None
            and directory_identity_attestation is None
        ):
            plan.action = "skip"
            plan.note = automatic_match_confirmation_message(
                automatic_policy.name
            )
            if match is not None:
                match.need_confirm = True
                match.error = plan.note
            return plan
        if not self._match_identity_key(match):
            plan.action = "skip"
            plan.note = "未识别"
            return plan

        from app.notifier import safe_int

        # 仅在 scraper 对象上真实声明统一解析接口时启用。
        # unittest.mock.Mock 会为任意属性动态生成可调用对象，普通 getattr/hasattr
        # 会误判为支持 parse_media；getattr_static 同时兼容类方法和显式实例方法。
        from inspect import getattr_static

        parse_media_declared = getattr_static(self.scraper, "parse_media", None)
        parse_media_impl = (
            getattr(self.scraper, "parse_media", None)
            if parse_media_declared is not None
            else None
        )
        if not callable(parse_media_impl):
            raise TypeError("scraper 必须实现 parse_media 统一识别接口")
        release_parse = parse_media_impl(file.name, parent_path, match)
        parsed: dict[str, object] = {
            "title": release_parse.title,
            "year": release_parse.year,
            "type": release_parse.media_type,
            "season": release_parse.effective_season,
            "episode": release_parse.effective_episode,
        }
        if release_parse.tmdb_id:
            parsed["tmdb_id"] = release_parse.tmdb_id
        if rules.nsfw_exclusive:
            from app.modules.nsfw import extract_nsfw_multipart
            multipart = extract_nsfw_multipart(
                file.name, rules.nsfw_strip_domains,
            )
            override_reader = getattr(self.scraper, "multipart_override", None)
            override = (
                override_reader(file.name, parent_path)
                if callable(override_reader) else None
            )
            if override is not None:
                plan.multipart_index = int(override)
                plan.multipart_token = f"CD{plan.multipart_index}"
            elif multipart is not None:
                plan.multipart_index = multipart.part_index
                plan.multipart_token = multipart.token
                plan.multipart_ambiguous = multipart.ambiguous
            if plan.multipart_index is not None:
                parsed["part"] = plan.multipart_index
        source_season = release_parse.source_season
        source_episode = release_parse.source_episode
        if source_position_override is not None:
            source_season, source_episode = source_position_override
        if effective_parsed_override is not None:
            parsed["season"], parsed["episode"] = effective_parsed_override
        parsed_season = safe_int(parsed.get("season"), None, minimum=0)
        parsed_episode = safe_int(parsed.get("episode"), None, minimum=0)
        source_season_value = safe_int(source_season, None, minimum=0)
        source_episode_value = safe_int(source_episode, None, minimum=0)
        detail = self._detail_for_match(match)
        season_title_mapping: EpisodeMappingPlan | None = None

        if (
            automatic
            and match.media_type == "tv"
            and parsed_season not in (None, 0)
            and parsed_episode is not None
            and implicit_season_conflicts_with_candidate_title(
                file.name, parent_path, match, detail
            )
        ):
            message = "标题尾部数字同时属于媒体正式名称，无法安全确定季号，需人工确认"
            match.need_confirm = True
            match.status = "low_confidence"
            match.error = message
            plan.action = "skip"
            plan.note = message
            plan.episode = parsed_episode
            return plan

        if match.media_type == "tv" and parsed_episode is not None and parsed_season is None:
            # 季标题来自 TMDB seasons 结构，只能用于已确认的 TMDB 候选。
            # TVDB/BGM 等 provider 的 detail 结构可能恰好含同名字段，但不能
            # 因此跨 provider 套用 TMDB 季号语义。
            season_title_hint = infer_tmdb_season_from_title_evidence(
                file.name,
                parent_path,
                detail,
                episode=parsed_episode,
            )
            tmdb_identity = (
                isinstance(self.scraper, TMDBScraper)
                and self._match_provider(match) == "tmdb"
            )
            if season_title_hint is not None and not tmdb_identity and automatic:
                message = "检测到季标题线索，但当前候选不是 TMDB 身份，无法安全换算季号，需人工确认"
                match.need_confirm = True
                match.status = "low_confidence"
                match.error = message
                plan.action = "skip"
                plan.note = message
                plan.episode = parsed_episode
                return plan
            inferred_season = season_title_hint if tmdb_identity else None
            if inferred_season is not None:
                inferred_validation = (
                    self.scraper.validate_position(
                        detail,
                        match.media_type,
                        inferred_season,
                        parsed_episode,
                    )
                    if isinstance(self.scraper, TMDBScraper)
                    and self._match_provider(match) == "tmdb"
                    else {"required": False, "passed": True}
                )
                if (
                    not inferred_validation.get("required")
                    or inferred_validation.get("passed")
                ):
                    parsed_season = inferred_season
                    season_title_mapping = EpisodeMappingPlan(
                        source_season_value,
                        source_episode_value or parsed_episode,
                        inferred_season,
                        parsed_episode,
                        mode="season_title",
                        reason="tmdb_season_title_evidence",
                        confidence=0.98,
                    )
            if (
                parsed_season is None
                and automatic
                and has_unresolved_season_hint(file.name, parent_path)
            ):
                message = "检测到续作/分部标记但无法安全确定 TMDB 季号，需人工确认"
                match.need_confirm = True
                match.status = "low_confidence"
                match.error = message
                plan.action = "skip"
                plan.note = message
                plan.episode = parsed_episode
                return plan
            if (
                parsed_season is None
                and automatic
                and release_parse is not None
                and len(season_episode_counts(detail)) > 1
                and has_unresolved_candidate_title_remainder(
                    release_parse.context, match, detail
                )
            ):
                message = "发布标题仍包含未解释的篇章信息，无法安全确定 TMDB 季号，需人工确认"
                match.need_confirm = True
                match.status = "low_confidence"
                match.error = message
                plan.action = "skip"
                plan.note = message
                plan.episode = parsed_episode
                return plan
            if (
                parsed_season is None
                and automatic
                and not explicit_tmdb_id
                and directory_episode_evidence is None
                and source_season_value is None
                and parsed_episode > 24
            ):
                message = "孤立高集号缺少季号或连续目录证据，已阻止自动整理"
                match.need_confirm = True
                match.status = "low_confidence"
                match.error = message
                plan.action = "skip"
                plan.note = message
                plan.episode = parsed_episode
                return plan
            if parsed_season is None:
                parsed_season = 1
        parsed["season"] = parsed_season
        parsed["episode"] = parsed_episode
        resolved_mapping = self._metadata_episode_mapping(
            match,
            source_season=source_season_value,
            source_episode=source_episode_value,
            parsed_season=parsed_season,
            parsed_episode=parsed_episode,
        )
        if resolved_mapping is not None:
            plan.source_season = resolved_mapping.source_season
            plan.source_episode = resolved_mapping.source_episode
            plan.episode_mapping = resolved_mapping
        elif season_title_mapping is not None:
            plan.source_season = season_title_mapping.source_season
            plan.source_episode = season_title_mapping.source_episode
            plan.episode_mapping = season_title_mapping
        else:
            plan.source_season = safe_int(source_season, parsed_season, minimum=0)
            plan.source_episode = safe_int(source_episode, parsed_episode, minimum=0)
        manual_position_confirmed = bool(
            getattr(self.scraper, "manual_position_confirmed", False)
        )
        release_position = parse_release_position(file.name)
        range_start = safe_int(release_position.get("episode"), None, minimum=1)
        episode_end = safe_int(release_position.get("episode_end"), None, minimum=1)
        if (
            not manual_position_confirmed
            and episode_end is not None
            and range_start is not None
            and episode_end > range_start
        ):
            message = (
                f"检测到多集文件 E{range_start:02d}-E{episode_end:02d}，"
                "当前单文件命名不能安全表达范围，需人工确认"
            )
            match.need_confirm = True
            match.status = "low_confidence"
            match.error = message
            plan.action = "skip"
            plan.note = message
            plan.season = parsed_season
            plan.episode = range_start
            return plan
        if (
            parsed_override is None
            and not manual_position_confirmed
            and match.media_type == "tv"
            and parsed_episode is None
        ):
            message = (
                f"剧集文件缺少集数，不能自动归档: {file.name}（无法确定集号）"
            )
            match.need_confirm = True
            match.status = "low_confidence"
            match.error = message
            plan.action = "skip"
            plan.note = message
            plan.season = parsed_season
            return plan
        if (
            (parsed_override is None or directory_episode_evidence is not None)
            and type(self.scraper) is TMDBScraper
            and self._match_provider(match) == "tmdb"
            and match.media_type == "tv"
            and plan.episode_mapping is None
            and plan.source_season not in (None, 0)
            and plan.source_episode is not None
        ):
            mapping = infer_episode_mapping(
                source_season=plan.source_season,
                source_episode=plan.source_episode,
                parent_path=parent_path,
                detail=detail,
                mode="auto",
                directory_evidence=directory_episode_evidence,
            )
            if (
                automatic
                and explicit_tmdb_id
                and not mapping.changed
                and source_season_value in (None, 1)
                and plan.source_season == 1
                and plan.source_episode is not None
            ):
                # 显式 TMDB 标记已同时绑定了媒体身份、详情响应 ID 与 TV 类型。
                # 因此当 S01/裸绝对集号在 TMDB 第一季越界时，可以在无需目录
                # 连续包证据的情况下尝试唯一的绝对集数映射。映射后的目标仍须
                # 通过最终 TMDB 季集校验；ID 错误、季不存在、总集数不足等继续
                # 失败关闭，不能仅凭标记绕过位置安全门。
                absolute_mapping = infer_episode_mapping(
                    source_season=1,
                    source_episode=plan.source_episode,
                    parent_path=parent_path,
                    detail=detail,
                    mode="absolute",
                )
                absolute_validation = self.scraper.validate_position(
                    detail,
                    match.media_type,
                    absolute_mapping.target_season,
                    absolute_mapping.target_episode,
                )
                if (
                    absolute_mapping.changed
                    and absolute_mapping.confidence >= 0.9
                    and absolute_validation.get("required")
                    and absolute_validation.get("passed")
                    and str(absolute_validation.get("reason") or "")
                    == "episode_verified"
                ):
                    mapping = EpisodeMappingPlan(
                        source_season_value,
                        plan.source_episode,
                        absolute_mapping.target_season,
                        absolute_mapping.target_episode,
                        mode=absolute_mapping.mode,
                        reason=absolute_mapping.reason,
                        confidence=absolute_mapping.confidence,
                        range_start=absolute_mapping.range_start,
                        range_end=absolute_mapping.range_end,
                    )
            split_cour_directory_evidence = bool(
                directory_episode_evidence is not None
                and directory_episode_evidence.contiguous
                and directory_episode_evidence.source_season == plan.source_season
                and directory_episode_evidence.range_start == 1
                and directory_episode_evidence.episode_count
                == directory_episode_evidence.range_end
                and plan.source_episode is not None
                and plan.source_episode <= directory_episode_evidence.range_end
            )
            if (
                automatic
                and (explicit_tmdb_id or split_cour_directory_evidence)
                and plan.source_season == 2
                and plan.source_episode is not None
            ):
                # 发布组可能把分割放送写成 S02E06，但 TMDB 将两段仍合并在
                # Season 01。目录从 E01 连续起步时，整包本身能够证明发布方
                # 第二季已重置集号；显式 TMDB 单文件则仅在源集号落在第二段
                # 起点之前时覆盖普通绝对映射，避免把 S02E13 这类连续编号
                # 错改成第二段第 13 集。最终目标仍须通过季集门禁。
                counts = season_episode_counts(detail)
                if len(counts) == 1:
                    merged_season = next(iter(counts))
                    season_detail = self.scraper.get_tv_season_detail(
                        str(explicit_tmdb_id or match.tmdb_id), merged_season
                    )
                    cour_mapping = infer_merged_season_cour_mapping(
                        source_season=plan.source_season,
                        source_episode=plan.source_episode,
                        detail=detail,
                        season_detail=season_detail,
                    )
                    cour_validation = self.scraper.validate_position(
                        detail,
                        match.media_type,
                        cour_mapping.target_season,
                        cour_mapping.target_episode,
                    )
                    reset_position_proven = bool(
                        split_cour_directory_evidence
                        or (
                            cour_mapping.range_start is not None
                            and plan.source_episode < cour_mapping.range_start
                        )
                    )
                    if (
                        reset_position_proven
                        and cour_mapping.changed
                        and cour_mapping.confidence >= 0.9
                        and cour_validation.get("required")
                        and cour_validation.get("passed")
                        and str(cour_validation.get("reason") or "")
                        == "episode_verified"
                    ):
                        mapping = cour_mapping
            plan.episode_mapping = mapping
            plan.source_season = mapping.source_season
            plan.source_episode = mapping.source_episode
            # ``recognition_name`` 只用于复用目录身份，携带的是首个被识别
            # 文件的位置。最终归档位置必须始终以当前文件经 TMDB 校验后的
            # mapping 为准；即使映射是 identity（例如源文件本来就是 S02E01），
            # 也要覆盖缓存身份中的位置，不能只在 ``changed`` 时更新。
            parsed_season = mapping.target_season
            parsed_episode = mapping.target_episode
            parsed["season"] = parsed_season
            parsed["episode"] = parsed_episode
            match.effective_season = parsed_season
            match.effective_episode = parsed_episode
            match.metadata = {
                **dict(getattr(match, "metadata", None) or {}),
                "episode_mapping": mapping.to_dict(),
            }
        automatic_proof_accepted = False
        directory_package_proof_accepted = False
        directory_identity_attestation_accepted = False
        if (
            isinstance(self.scraper, TMDBScraper)
            and self._match_provider(match) == "tmdb"
            and match.media_type == "tv"
            and parsed_season not in (None, 0)
        ):
            validation = self.scraper.validate_position(
                detail, match.media_type, parsed_season, parsed_episode
            )
            match.metadata = {
                **dict(getattr(match, "metadata", None) or {}),
                "final_position_validation": dict(validation),
            }
            if (
                automatic
                and validation.get("required")
                and not validation.get("passed")
                and str(validation.get("reason") or "")
                in {"season_not_found", "episode_out_of_range"}
            ):
                refreshed_detail, refreshed = self._refresh_tmdb_detail_once(match)
                if refreshed and refreshed_detail:
                    detail = refreshed_detail
                    validation = self.scraper.validate_position(
                        detail, match.media_type, parsed_season, parsed_episode
                    )
                    match.metadata = {
                        **dict(getattr(match, "metadata", None) or {}),
                        "final_position_validation": dict(validation),
                        "tmdb_detail_force_refreshed": True,
                    }
            if validation.get("required") and not validation.get("passed"):
                message = self.scraper.position_validation_error(validation)
                match.need_confirm = True
                match.status = "low_confidence"
                match.error = message
                constraint = f"tmdb_position_{validation.get('reason') or 'unverified'}"
                rejected = getattr(match, "rejected_constraints", None)
                if isinstance(rejected, list) and constraint not in rejected:
                    rejected.append(constraint)
                threshold_decision = dict(
                    getattr(match, "threshold_decision", None) or {}
                )
                threshold_decision.update({
                    "passed": False,
                    "reason": constraint,
                })
                match.threshold_decision = threshold_decision
                plan.action = "skip"
                plan.note = message
                plan.season = parsed_season
                plan.episode = parsed_episode
                return plan
            if directory_identity_attestation is not None:
                attested_tmdb_id = str(
                    directory_identity_attestation.get("tmdb_id") or ""
                ).strip()
                match_tmdb_id = str(getattr(match, "tmdb_id", "") or "").strip()
                match_external_id = str(
                    getattr(match, "external_id", "") or ""
                ).strip()
                detail_tmdb_id = str(
                    detail.get("id") or ""
                ).strip() if isinstance(detail, dict) else ""
                directory_identity_attestation_accepted = bool(
                    int(directory_identity_attestation.get("version") or 0)
                    == _DIRECTORY_IDENTITY_ATTESTATION_VERSION
                    and directory_identity_attestation.get("kind")
                    == "same_scan_tmdb_tv_identity"
                    and directory_identity_attestation.get("provider") == "tmdb"
                    and directory_identity_attestation.get("media_type") == "tv"
                    and attested_tmdb_id
                    and attested_tmdb_id == match_tmdb_id
                    and (not match_external_id or match_external_id == match_tmdb_id)
                    and detail_tmdb_id == attested_tmdb_id
                    and validation.get("required")
                    and validation.get("passed")
                    and str(validation.get("reason") or "") == "episode_verified"
                    and parsed_season not in (None, 0)
                    and parsed_episode is not None
                )
                if not directory_identity_attestation_accepted:
                    message = "目录身份凭证未通过当前文件的最终季集复核，需人工确认"
                    match.need_confirm = True
                    match.status = "low_confidence"
                    match.error = message
                    plan.action = "skip"
                    plan.note = message
                    plan.season = parsed_season
                    plan.episode = parsed_episode
                    return plan
                match.need_confirm = False
                match.status = "matched"
                match.error = ""
                match.metadata = {
                    **dict(getattr(match, "metadata", None) or {}),
                    _DIRECTORY_IDENTITY_ATTESTATION_ACCEPTED_KEY: {
                        "tmdb_id": attested_tmdb_id,
                        "season": parsed_season,
                        "episode": parsed_episode,
                        "position_validation": dict(validation),
                    },
                }
            if (
                automatic_proof is not None
                and directory_identity_attestation is None
            ):
                # 同目录首个文件已建立作品身份凭证后，后续文件必须以当前
                # 源文件解析位置 + TMDB 最终校验为准；合成识别名携带的 E01
                # 证明只属于首个搜索输入，不能再次约束 E02/E03。
                target_position = automatic_proof.get("target_position")
                proof_tmdb_id = str(automatic_proof.get("external_id") or "").strip()
                match_tmdb_id = str(getattr(match, "tmdb_id", "") or "").strip()
                match_external_id = str(
                    getattr(match, "external_id", "") or ""
                ).strip()
                detail_tmdb_id = str(
                    detail.get("id") or ""
                ).strip() if isinstance(detail, dict) else ""
                try:
                    proof_season = int(
                        target_position.get("season")
                        if isinstance(target_position, dict) else None
                    )
                    proof_episode = int(
                        target_position.get("episode")
                        if isinstance(target_position, dict) else None
                    )
                except (TypeError, ValueError):
                    proof_season = proof_episode = -1

                season_year_evidence = automatic_proof.get(
                    "target_season_year_evidence"
                )
                season_year_detail_valid = True
                if season_year_evidence is not None:
                    season_year_detail_valid = False
                    if isinstance(season_year_evidence, dict):
                        expected_year = str(
                            season_year_evidence.get("expected_year") or ""
                        ).strip()
                        evidence_air_date = str(
                            season_year_evidence.get("season_air_date") or ""
                        ).strip()
                        current_season = next((
                            item for item in (detail.get("seasons") or [])
                            if isinstance(item, dict)
                            and safe_int(item.get("season_number"), None, minimum=0)
                            == proof_season
                        ), None)
                        current_air_date = str(
                            current_season.get("air_date") or ""
                        ).strip() if isinstance(current_season, dict) else ""
                        season_year_detail_valid = bool(
                            re.fullmatch(r"(?:19|20)\d{2}", expected_year)
                            and current_air_date[:4] == expected_year
                            and evidence_air_date[:4] == expected_year
                            and str(season_year_evidence.get("tmdb_id") or "").strip()
                            == proof_tmdb_id
                        )

                automatic_proof_accepted = bool(
                    proof_tmdb_id
                    and proof_tmdb_id == match_tmdb_id
                    and (not match_external_id or match_external_id == match_tmdb_id)
                    and detail_tmdb_id == proof_tmdb_id
                    and validation.get("required")
                    and validation.get("passed")
                    and str(validation.get("reason") or "") == "episode_verified"
                    and parsed_season == proof_season
                    and parsed_episode == proof_episode
                    and season_year_detail_valid
                )
                if not automatic_proof_accepted:
                    message = "自动识别证据与最终季集位置不一致，需人工确认"
                    match.need_confirm = True
                    match.status = "low_confidence"
                    match.error = message
                    plan.action = "skip"
                    plan.note = message
                    plan.season = parsed_season
                    plan.episode = parsed_episode
                    return plan
                match.need_confirm = False
                match.status = "matched"
                match.error = ""
                match.metadata = {
                    **dict(getattr(match, "metadata", None) or {}),
                    "verified_automatic_identity_proof_accepted": {
                        "tmdb_id": proof_tmdb_id,
                        "season": parsed_season,
                        "episode": parsed_episode,
                        "position_validation": dict(validation),
                    },
                }
            if directory_package_proof is not None:
                proof_tmdb_id = str(
                    directory_package_proof.get("tmdb_id") or ""
                ).strip()
                match_tmdb_id = str(getattr(match, "tmdb_id", "") or "").strip()
                match_external_id = str(
                    getattr(match, "external_id", "") or ""
                ).strip()
                detail_tmdb_id = str(
                    detail.get("id") or ""
                ).strip() if isinstance(detail, dict) else ""
                directory_package_proof_accepted = bool(
                    proof_tmdb_id
                    and proof_tmdb_id == match_tmdb_id
                    and match_external_id == match_tmdb_id
                    and detail_tmdb_id == proof_tmdb_id
                    and validation.get("required")
                    and validation.get("passed")
                    and str(validation.get("reason") or "")
                    == "episode_verified"
                    and parsed_season not in (None, 0)
                    and parsed_episode is not None
                )
                if not directory_package_proof_accepted:
                    message = "连续剧集包身份凭证未通过最终季集复核，需人工确认"
                    match.need_confirm = True
                    match.status = "low_confidence"
                    match.error = message
                    plan.action = "skip"
                    plan.note = message
                    plan.season = parsed_season
                    plan.episode = parsed_episode
                    return plan
                match.need_confirm = False
                match.status = "matched"
                match.error = ""
                match.metadata = {
                    **dict(getattr(match, "metadata", None) or {}),
                    _DIRECTORY_PACKAGE_IDENTITY_ACCEPTED_KEY: {
                        "tmdb_id": proof_tmdb_id,
                        "season": parsed_season,
                        "episode": parsed_episode,
                        "position_validation": dict(validation),
                    },
                }
        if (
            automatic_proof is not None
            and directory_identity_attestation is None
            and not automatic_proof_accepted
        ):
            message = "自动识别强证据未完成最终季集复核，需人工确认"
            match.need_confirm = True
            match.status = "low_confidence"
            match.error = message
            plan.action = "skip"
            plan.note = message
            plan.season = parsed_season
            plan.episode = parsed_episode
            return plan
        if (
            directory_package_proof is not None
            and not directory_package_proof_accepted
        ):
            message = "连续剧集包身份凭证未完成最终季集复核，需人工确认"
            match.need_confirm = True
            match.status = "low_confidence"
            match.error = message
            plan.action = "skip"
            plan.note = message
            plan.season = parsed_season
            plan.episode = parsed_episode
            return plan
        self._populate_move_plan(
            plan,
            file,
            rules,
            match,
            detail,
            parsed,
            parsed_season=parsed_season,
            automatic=automatic,
            media_probe_cache_only=media_probe_cache_only,
            media_probe_cached_payload=media_probe_cached_payload,
            media_probe_cache_prefetched=media_probe_cache_prefetched,
        )
        plan.action = "move"
        return plan

    def _companions_for_plan(self, plan: OrganizePlan,
                             candidates: list[GuangYaFile]) -> list[GuangYaFile]:
        """仅选择与当前视频 basename 明确相关的字幕和元数据。"""
        video_stem = normalized_stem(plan.original_name)
        matched = []
        semantic_image_suffixes = {
            "poster", "fanart", "backdrop", "banner", "thumb", "thumbnail",
            "landscape", "logo", "clearlogo", "clearart", "disc", "discart",
            "folder",
        }
        for item in candidates:
            metadata_stem = normalized_stem(item.name)
            exact_match = metadata_stem == video_stem
            suffix = (
                metadata_stem[len(video_stem):]
                if video_stem and metadata_stem.startswith(video_stem)
                else ""
            )
            semantic_image_match = (
                media_role(item.name) == "image"
                and suffix in semantic_image_suffixes
            )
            if exact_match or semantic_image_match:
                matched.append(item)
        return matched

    def _replacement_verification_snapshot(
        self, target_id: str, old_file_id: str, new_file_id: str,
        *, attempts: int = 3,
    ) -> tuple[list[GuangYaFile], GuangYaFile | None, GuangYaFile | None]:
        """短暂重读云端目录，吸收移动后的最终一致性延迟。"""
        refreshed: list[GuangYaFile] = []
        old_detail = None
        new_detail = None
        file_info = getattr(self.client, "file_info", None)
        for attempt in range(max(1, attempts)):
            refreshed = self.client.list_dir(target_id)
            old_detail = file_info(old_file_id) if callable(file_info) else None
            new_detail = file_info(new_file_id) if callable(file_info) else None
            visible_ids = {item.file_id for item in refreshed}
            if old_file_id in visible_ids and new_file_id in visible_ids:
                break
            if attempt + 1 < attempts:
                time.sleep(0.15 * (attempt + 1))
        return refreshed, old_detail, new_detail

    @staticmethod
    def _release_fields(release_parse: object) -> dict[str, object]:
        return {
            "title": str(getattr(release_parse, "title", "") or ""),
            "year": str(getattr(release_parse, "year", "") or ""),
            "type": str(getattr(release_parse, "media_type", "") or ""),
            "tmdb_id": str(getattr(release_parse, "tmdb_id", "") or ""),
            "season": normalize_media_number(
                getattr(release_parse, "effective_season", None)
            ),
            "episode": normalize_media_number(
                getattr(release_parse, "effective_episode", None)
            ),
        }

    def _parse_media_fields(self, name: str) -> dict[str, object]:
        try:
            return self._release_fields(self.scraper.parse_media(name))
        except (AttributeError, TypeError, ValueError):
            return {}

    def _parse_existing_media_fields(self, name: str) -> dict[str, object]:
        parser = getattr(self.scraper, "parse_existing_media", None)
        if callable(parser):
            try:
                return self._release_fields(parser(name))
            except (AttributeError, TypeError, ValueError):
                pass
        return self._parse_media_fields(name)

    def _season_episode_inventory(
        self,
        files: list[GuangYaFile],
        rules: OrganizeRules,
        *,
        season: int | None,
    ) -> list[int] | None:
        """从已缓存的归档季目录得到真实集号；不增加云盘请求。"""
        if season is None:
            return None
        episodes: set[int] = set()
        for item in files:
            if item.is_dir:
                continue
            extension = item.name.rsplit(".", 1)[-1].lower() if "." in item.name else ""
            if extension not in self.video_exts(rules):
                continue
            parsed = self._parse_existing_media_fields(item.name)
            parsed_season = normalize_media_number(parsed.get("season"))
            parsed_episode = normalize_media_number(parsed.get("episode"))
            if parsed_episode is None or parsed_episode <= 0:
                continue
            if parsed_season is not None and parsed_season != season:
                continue
            episodes.add(parsed_episode)
        return sorted(episodes)

    def _same_media_identity(self, plan: OrganizePlan, candidate: GuangYaFile,
                             rules: OrganizeRules) -> bool:
        """目标媒体目录内按电影或剧集集号识别同一媒体，不比较技术规格。"""
        if candidate.is_dir:
            return False
        ext = candidate.name.rsplit(".", 1)[-1].lower() if "." in candidate.name else ""
        if ext not in self.video_exts(rules):
            return False
        if not plan.match:
            return False
        if plan.match.media_type != "tv":
            auxiliary = re.search(
                r"(?i)(?:^|[._ \-])(?:trailer|teaser|sample|preview|"
                r"behind[._ \-]*the[._ \-]*scenes|deleted[._ \-]*scenes?|"
                r"featurettes?|extras?|interviews?|花絮|预告|彩蛋)"
                r"(?:[._ \-]|$)",
                candidate.name,
            )
            if auxiliary:
                return False
            if self._match_provider(plan.match) in {"metatube", "clean_title"}:
                from app.modules.nsfw import extract_nsfw_part_index
                candidate_part = extract_nsfw_part_index(candidate.name)
                if plan.multipart_index is not None or candidate_part is not None:
                    return plan.multipart_index == candidate_part
                # 成人媒体目录已由 provider + 番号身份隔离；目录内未分段视频
                # 视为同一作品的版本候选，继续沿用现有冲突策略。
                return True
            candidate_fields = self._parse_existing_media_fields(candidate.name)
            candidate_tmdb_id = str(candidate_fields.get("tmdb_id") or "")
            if not candidate_tmdb_id:
                tmdb_match = re.search(
                    r"[\{\(]tmdb-(\d+)[\}\)]",
                    candidate.name,
                    re.IGNORECASE,
                )
                candidate_tmdb_id = tmdb_match.group(1) if tmdb_match else ""
            if candidate_tmdb_id:
                candidate_media_type = str(
                    candidate_fields.get("type")
                    or candidate_fields.get("media_type")
                    or ""
                ).lower()
                planned_media_type = str(plan.match.media_type or "movie").lower()
                if (
                    candidate_media_type in {"movie", "tv"}
                    and candidate_media_type != planned_media_type
                ):
                    return False
                return bool(
                    plan.match.tmdb_id
                    and candidate_tmdb_id == str(plan.match.tmdb_id)
                )
            planned_fields = self._parse_media_fields(plan.new_name or plan.original_name)
            candidate_title = str(candidate_fields.get("title") or "")
            planned_title = str(
                planned_fields.get("title") or plan.match.title or ""
            )
            candidate_year = str(candidate_fields.get("year") or "")
            planned_year = str(
                planned_fields.get("year") or plan.match.year or plan.year or ""
            )
            if not candidate_year:
                year_match = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", candidate.name)
                candidate_year = year_match.group(1) if year_match else ""
            normalized_planned_title = normalized_stem(planned_title)
            if candidate_title:
                title_matches = (
                    normalized_stem(candidate_title) == normalized_planned_title
                )
            else:
                title_matches = bool(
                    normalized_planned_title
                    and normalized_stem(candidate.name).startswith(
                        normalized_planned_title
                    )
                )
            if not (title_matches and candidate_year and planned_year):
                return False
            return bool(
                plan.match.tmdb_id
                and candidate_year == planned_year
            )
        if plan.season is None or plan.episode is None:
            return False
        parsed = self._parse_existing_media_fields(candidate.name)
        return (
            parsed.get("season") is not None
            and parsed.get("episode") is not None
            and
            parsed.get("season") == plan.season
            and parsed.get("episode") == plan.episode
        )

    def _best_same_variant(
        self,
        candidates: list[GuangYaFile],
        rules: OrganizeRules,
        evidence_names: dict[str, str] | None = None,
    ) -> GuangYaFile:
        """从同版本候选中按既有评分、大小和稳定 file-id 选择当前赢家。"""
        ordered = sorted(candidates, key=lambda item: str(item.file_id))
        winner = ordered[0]
        evidence_names = evidence_names or {}
        for candidate in ordered[1:]:
            if self.should_replace(
                winner,
                candidate,
                candidate.name,
                rules,
                existing_evidence=evidence_names.get(winner.file_id, winner.name),
                incoming_evidence=evidence_names.get(candidate.file_id, candidate.name),
            ):
                winner = candidate
        return winner

    def _prime_existing_variant_cache(
        self,
        files: list[GuangYaFile],
        rules: OrganizeRules,
        evidence_names: dict[str, str] | None = None,
    ) -> tuple[int, int]:
        """批量预热目标文件版本缓存；失败时保留逐文件读取兜底。"""
        if not rules.media_probe_enabled:
            return 0, 0
        evidence_names = evidence_names or {}
        candidates: list[tuple[GuangYaFile, str, tuple[str, str, int, str]]] = []
        pending_versions: list[tuple[str, str, int]] = []
        for file in files:
            if file.is_dir or not file.etag or not file.size:
                continue
            evidence = str(evidence_names.get(file.file_id) or file.name)
            cache_key = (
                str(file.file_id), str(file.etag or ""), int(file.size or 0), evidence
            )
            if cache_key in self._existing_variant_cache:
                continue
            candidates.append((file, evidence, cache_key))
            version_key = cache_key[:3]
            if version_key not in self._media_probe_cache_checked:
                pending_versions.append(version_key)
        batches = 0
        hits = 0
        if pending_versions:
            try:
                cached_payloads = db.get_media_probe_cache_many(
                    pending_versions, allow_fingerprint_fallback=True
                )
                batches = 1
                self._media_probe_cache_checked.update(pending_versions)
                self._media_probe_payload_cache.update(cached_payloads)
                hits = len(cached_payloads)
            except Exception as exc:
                logger.warning(
                    "批量预热目标媒体规格失败，回退逐文件读取 type=%s",
                    type(exc).__name__,
                )
                return 0, 0
        for _file, evidence, cache_key in candidates:
            payload = self._media_probe_payload_cache.get(cache_key[:3], "")
            try:
                profile = json.loads(payload) if payload else None
            except (TypeError, ValueError):
                profile = None
            self._existing_variant_cache[cache_key] = classify_variant(
                evidence, profile if isinstance(profile, dict) else None
            )
        return batches, hits

    def _existing_variant(
        self,
        file: GuangYaFile,
        rules: OrganizeRules,
        evidence_name: str = "",
    ) -> MediaVariant:
        evidence = str(evidence_name or file.name)
        version_key = (
            str(file.file_id), str(file.etag or ""), int(file.size or 0)
        )
        cache_key = (*version_key, evidence)
        cached_variant = self._existing_variant_cache.get(cache_key)
        if cached_variant is not None:
            return cached_variant
        profile = None
        if rules.media_probe_enabled and file.etag and file.size:
            if version_key not in self._media_probe_cache_checked:
                cached = get_media_probe_cache(
                    *version_key, allow_fingerprint_fallback=True
                )
                self._media_probe_cache_checked.add(version_key)
                if cached:
                    self._media_probe_payload_cache[version_key] = cached
            cached = self._media_probe_payload_cache.get(version_key, "")
            if cached:
                try:
                    payload = json.loads(cached)
                    profile = payload if isinstance(payload, dict) else None
                except (TypeError, ValueError):
                    profile = None
        variant = classify_variant(evidence, profile)
        self._existing_variant_cache[cache_key] = variant
        return variant

    def _resolve_variant_conflict(
        self,
        plan: OrganizePlan,
        target_files: list[GuangYaFile],
        rules: OrganizeRules,
        evidence_names: dict[str, str] | None = None,
    ) -> tuple[GuangYaFile | None, str, str]:
        """用同一判定供预览和执行选择新建、共存、替换或跳过。"""
        incoming = GuangYaFile(
            file_id=plan.file_id,
            name=plan.original_name,
            is_dir=False,
            size=plan.size,
            etag=plan.etag,
        )
        evidence_names = evidence_names or {}
        same_variant: list[GuangYaFile] = []
        coexist_count = 0
        for candidate in target_files:
            # 归档目录被再次作为待整理来源时，目标列表可能包含计划文件自身。
            # 自身绝不能参与版本替换，否则会把同一 file_id 当旧版本回收。
            if (
                plan.file_id
                and str(candidate.file_id or "") == str(plan.file_id)
            ):
                continue
            if not self._same_media_identity(plan, candidate, rules):
                continue
            existing_variant = self._existing_variant(
                candidate,
                rules,
                evidence_names.get(candidate.file_id, candidate.name),
            )
            if variants_can_coexist(existing_variant, plan.variant, rules):
                coexist_count += 1
            else:
                same_variant.append(candidate)

        if same_variant:
            existing = self._best_same_variant(same_variant, rules, evidence_names)
            if self.should_replace(
                existing,
                incoming,
                plan.new_name,
                rules,
                existing_evidence=evidence_names.get(existing.file_id, existing.name),
                incoming_evidence=f"{plan.original_name} {plan.new_name}",
            ):
                return existing, "replace", "同版本仍按冲突策略处理：新文件胜出并替换现有版本"
            return existing, "skip", "同版本仍按冲突策略处理：保留现有版本并跳过新文件"
        if coexist_count:
            return None, "coexist", "不同版本允许共存；同版本仍按冲突策略处理"
        return None, "new", "未发现同媒体版本，可直接归档；同版本仍按冲突策略处理"

    def _find_existing_dir_chain(
        self, root_id: str, path: str,
        listing_cache: dict[str, list[GuangYaFile]] | None = None,
    ) -> str | None:
        """只按目标路径逐层读取；预览可复用公共祖先列表，执行仍会重新读取。"""
        current = root_id
        for part in (item for item in path.split("/") if item):
            entries = None if listing_cache is None else listing_cache.get(current)
            if entries is None:
                entries = self.client.list_dir(current)
                if listing_cache is not None:
                    listing_cache[current] = entries
            matches = [
                item for item in entries
                if item.is_dir and item.name == part
            ]
            if not matches:
                return None
            if len(matches) > 1:
                raise DirectoryScrapeConflictError(
                    f"归档目标存在重复同名目录：{part}，请先在网盘中合并或更名"
                )
            current = matches[0].file_id
        return current

    @classmethod
    def _plan_identity(cls, plan: OrganizePlan) -> tuple[str, str]:
        match = plan.match or MatchResult()
        return str(match.media_type or ""), cls._match_identity_key(match)

    @staticmethod
    def _identity_marker(path: str) -> str:
        match = re.search(
            r"[\{\(]((?:tmdb-\d+)|(?:(?:metatube|clean_title)-[A-Za-z0-9._-]+))[\}\)]",
            str(path or ""), re.IGNORECASE,
        )
        return "{" + match.group(1).lower() + "}" if match else ""

    def _historical_root_identities(self, media_root_path: str) -> set[tuple[str, str]]:
        prefix = str(media_root_path or "").strip("/")
        if not prefix:
            return set()
        result: set[tuple[str, str]] = set()
        for row in db.list_organize_root_identities(prefix):
            media_type = str(row["media_type"] or "")
            provider = str(row["provider"] or "").strip().lower() if "provider" in row.keys() else ""
            external_id = str(row["external_id"] or "").strip() if "external_id" in row.keys() else ""
            tmdb_id = str(row["tmdb_id"] or "").strip()
            if not provider and tmdb_id:
                provider, external_id = "tmdb", tmdb_id
            if media_type and provider and external_id:
                result.add((media_type, f"{provider}:{external_id}"))
        return result

    def _identity_guard_reason(
        self,
        plan: OrganizePlan,
        batch_bindings: dict[str, tuple[str, str]],
        history_cache: dict[str, set[tuple[str, str]]],
        identity_history_loader: Callable[
            [OrganizePlan], set[tuple[str, str]]
        ] | None = None,
    ) -> str:
        if not plan.identity_guard_required or not plan.media_root_path:
            return ""
        identity = self._plan_identity(plan)
        if not all(identity):
            return "目录模板未包含媒体身份标识，且当前媒体身份不完整，已阻止归档"
        bound = batch_bindings.get(plan.media_root_path)
        if bound and bound != identity:
            return "目录模板未包含媒体身份标识，同名目录对应不同媒体，已阻止混入"
        marker = self._identity_marker(plan.media_root_path)
        expected_marker = self._match_identity_tag(plan.match).lower()
        if marker and expected_marker and marker != expected_marker:
            return "目标目录媒体身份标识与当前媒体不一致，已阻止混入"
        if plan.media_root_path not in history_cache:
            if identity_history_loader is None:
                known_identities = self._historical_root_identities(
                    plan.media_root_path
                )
            else:
                known_identities = identity_history_loader(plan)
            history_cache[plan.media_root_path] = set(known_identities or set())
        known = history_cache[plan.media_root_path]
        if any(existing != identity for existing in known):
            return "历史整理记录显示该目录属于其他媒体，已阻止混入"
        batch_bindings.setdefault(plan.media_root_path, identity)
        return ""

    def _apply_identity_guards(
        self,
        plans: list[OrganizePlan],
        *,
        identity_history_loader: Callable[
            [OrganizePlan], set[tuple[str, str]]
        ] | None = None,
    ) -> None:
        batch_bindings: dict[str, tuple[str, str]] = {}
        history_cache: dict[str, set[tuple[str, str]]] = {}
        for plan in plans:
            if plan.action != "move":
                continue
            reason = self._identity_guard_reason(
                plan, batch_bindings, history_cache, identity_history_loader,
            )
            if not reason:
                continue
            plan.action = "conflict"
            plan.conflict_decision = "identity_blocked"
            plan.conflict_note = reason
            plan.note = reason

    def _preview_conflicts_with_inventory(
        self,
        plans: list[OrganizePlan],
        rules: OrganizeRules,
        inventory_loader: Callable[
            [OrganizePlan], tuple[str | None, list[GuangYaFile], dict[str, str]]
        ],
        *,
        identity_history_loader: Callable[
            [OrganizePlan], set[tuple[str, str]]
        ] | None = None,
    ) -> None:
        """使用来源适配器提供的只读目标库存执行统一冲突仲裁。"""
        self._apply_identity_guards(
            plans, identity_history_loader=identity_history_loader,
        )
        inventory_cache: dict[
            str, tuple[str | None, list[GuangYaFile], dict[str, str]]
        ] = {}
        batch_plans_by_file_id: dict[str, OrganizePlan] = {}
        for plan in plans:
            if plan.action != "move":
                continue
            try:
                target_id, loaded_files, loaded_evidence = inventory_loader(plan)
                inventory_key = str(target_id or plan.target_path or "")
                if inventory_key not in inventory_cache:
                    target_files = list(loaded_files or [])
                    evidence_names = dict(loaded_evidence or {})
                    self._prime_existing_variant_cache(
                        target_files, rules, evidence_names
                    )
                    inventory_cache[inventory_key] = (
                        target_id, target_files, evidence_names
                    )
                target_id, target_files, evidence_names = inventory_cache[inventory_key]
                if (
                    plan.original_parent_id
                    and target_id
                    and str(plan.original_parent_id) == str(target_id)
                ):
                    note = "文件已位于目标目录，未执行重复移动、覆盖或回收"
                    plan.action = "skip"
                    plan.conflict_decision = "already_organized"
                    plan.conflict_note = note
                    plan.note = note
                    continue
                existing, decision, note = self._resolve_variant_conflict(
                    plan, target_files, rules, evidence_names
                )
                previous_plan = (
                    batch_plans_by_file_id.get(str(existing.file_id or ""))
                    if existing is not None else None
                )
                plan.conflict_decision = decision
                plan.conflict_note = note
                if previous_plan is None:
                    plan.conflict_existing_id = str(
                        existing.file_id if existing is not None else ""
                    )
                    plan.conflict_existing_name = str(
                        existing.name if existing is not None else ""
                    )
                else:
                    # 批内候选只是只读仲裁时加入的虚拟库存，绝不能作为本地
                    # 事务的 retire_target。当前胜出者继承上一胜出者面对的真实
                    # 目标库存语义；目标库原本为空时继续保持 new/coexist。
                    plan.conflict_existing_id = previous_plan.conflict_existing_id
                    plan.conflict_existing_name = previous_plan.conflict_existing_name
                    if decision == "replace":
                        inherited = previous_plan.conflict_decision
                        plan.conflict_decision = (
                            inherited
                            if inherited in {"new", "coexist", "replace"}
                            else "new"
                        )
                        plan.conflict_note = (
                            f"{previous_plan.conflict_note}；批内同版本由当前更优版本胜出"
                            if previous_plan.conflict_note
                            else "批内同版本由当前更优版本胜出"
                        )
                if decision == "skip":
                    plan.action = "skip"
                    plan.note = note
                    if previous_plan is not None:
                        plan.conflict_existing_id = ""
                        plan.conflict_existing_name = ""
                elif decision in {"new", "coexist", "replace"}:
                    if existing is not None:
                        if previous_plan is not None:
                            batch_plans_by_file_id.pop(existing.file_id, None)
                            previous_plan.action = "skip"
                            previous_plan.conflict_decision = "batch_superseded"
                            previous_plan.conflict_note = (
                                "批内同版本冲突：由更优版本胜出，"
                                "未执行云盘写入或本地文件事务"
                            )
                            previous_plan.note = previous_plan.conflict_note
                        target_files[:] = [
                            item for item in target_files
                            if item.file_id != existing.file_id
                        ]
                        evidence_names.pop(existing.file_id, None)
                    target_files.append(GuangYaFile(
                        file_id=plan.file_id,
                        name=plan.new_name or plan.original_name,
                        is_dir=False,
                        size=plan.size,
                        etag=plan.etag,
                        parent_id=target_id or "0",
                    ))
                    evidence_names[plan.file_id] = (
                        f"{plan.original_name} {plan.new_name}"
                    )
                    batch_plans_by_file_id[plan.file_id] = plan
            except Exception as exc:
                logger.error(
                    "目标版本扫描失败 target_path=%s type=%s",
                    plan.target_path,
                    type(exc).__name__,
                )
                plan.action = "conflict"
                plan.conflict_decision = "blocked"
                plan.conflict_note = "目标版本扫描失败，禁止替换"
                plan.note = plan.conflict_note

    def _preview_conflicts(self, plans: list[OrganizePlan], rules: OrganizeRules) -> None:
        """按计划涉及的光鸭目标目录做有界只读检查。"""
        directory_cache: dict[
            str, tuple[str | None, list[GuangYaFile], dict[str, str]]
        ] = {}
        listing_cache: dict[str, list[GuangYaFile]] = {}

        def load_inventory(
            plan: OrganizePlan,
        ) -> tuple[str | None, list[GuangYaFile], dict[str, str]]:
            cached = directory_cache.get(plan.target_path)
            if cached is not None:
                return cached
            target_id = self._find_existing_dir_chain(
                rules.target_dir_id, plan.target_path, listing_cache
            )
            if target_id:
                target_files = listing_cache.get(target_id)
                if target_files is None:
                    target_files = self.client.list_dir(target_id)
                    listing_cache[target_id] = target_files
            else:
                target_files = []
            evidence_names = {
                item.file_id: item.name for item in target_files if not item.is_dir
            }
            result = (target_id, target_files, evidence_names)
            directory_cache[plan.target_path] = result
            return result

        self._preview_conflicts_with_inventory(plans, rules, load_inventory)

    def _restore_remote_file(
        self,
        item: GuangYaFile,
        original_parent_id: str,
        known_current_name: str,
    ) -> None:
        """尽力恢复一次可能已在服务端提交、但客户端收到异常的移动/重命名。"""
        current_name = str(known_current_name or item.name)
        current_parent_id = ""
        state_verified = False
        try:
            current = self.client.file_info(item.file_id)
        except Exception:
            current = None
        if current is not None:
            state_verified = True
            current_name = str(current.name or current_name)
            current_parent_id = str(current.parent_id or "")

        errors: list[Exception] = []
        # 查询状态失败时按“服务端操作可能已经提交”处理：重命名和移动都
        # 使用幂等目标重放，避免客户端超时后留下半提交状态。
        if not state_verified or current_name != item.name:
            try:
                self.client.rename(item.file_id, item.name)
            except Exception as exc:
                errors.append(exc)
        if original_parent_id and current_parent_id != original_parent_id:
            try:
                self.client.move([item.file_id], original_parent_id)
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise errors[0]

    def _verify_remote_snapshot(self, expected: GuangYaFile, *, role: str) -> GuangYaFile:
        """在第一次写操作前复核远端对象，拒绝使用过期扫描快照。"""
        file_info = getattr(self.client, "file_info", None)
        if not callable(file_info):
            # 兼容只实现最小移动接口的自定义适配器；官方光鸭客户端始终
            # 提供 file_info，因此生产链路仍执行严格复核。
            log_throttled(
                logger, logging.WARNING, "organize-client-file-info-missing",
                "远端客户端不支持 file_info，无法执行写前快照复核",
            )
            return expected
        try:
            current = file_info(expected.file_id)
        except Exception as exc:
            raise DirectoryScrapeConflictError(
                f"{role}状态复核失败，请刷新目录后重试"
            ) from exc
        if current is not None and not isinstance(current, GuangYaFile):
            log_throttled(
                logger, logging.WARNING, "organize-client-file-info-invalid",
                "远端客户端 file_info 返回未知类型，无法执行写前快照复核",
            )
            return expected
        if current is None or current.is_dir:
            raise DirectoryScrapeConflictError(
                f"{role}已不存在或类型已变化，请刷新目录后重试"
            )
        mismatches: list[str] = []
        if str(getattr(current, "name", "") or "") != str(expected.name or ""):
            mismatches.append("文件名")
        if str(getattr(current, "parent_id", "") or "") != str(expected.parent_id or ""):
            mismatches.append("所在目录")
        if int(getattr(current, "size", 0) or 0) != int(expected.size or 0):
            mismatches.append("文件大小")
        expected_etag = str(expected.etag or "")
        if expected_etag and str(getattr(current, "etag", "") or "") != expected_etag:
            mismatches.append("ETag")
        if mismatches:
            raise DirectoryScrapeConflictError(
                f"{role}在预览后发生变化（{'、'.join(mismatches)}），请刷新目录后重试"
            )
        return current

    @staticmethod
    def _write_organize_audit(
        log_args: tuple,
        log_kwargs: dict,
        items: list[dict],
    ) -> int:
        """用一个 SQLite 事务写入主日志和明细；明细异常时保留不完整审计。"""
        item_error: Exception | None = None
        with db.get_conn() as conn:
            log_id = add_organize_log(*log_args, **log_kwargs, _conn=conn)
            try:
                add_organize_log_items(log_id, items, _conn=conn)
                if len(log_args) >= 5 and str(log_args[4] or "") == "success":
                    try:
                        db.resolve_pending_organize_logs(
                            str(log_args[0] or ""),
                            str(log_args[3] or ""),
                            before_log_id=log_id,
                            _conn=conn,
                        )
                    except Exception as resolve_exc:
                        # 前置待确认记录的展示结算不能反向污染已经成功的
                        # 云端移动；时间线仍可通过兼容查询折叠旧记录。
                        logger.warning(
                            "结算人工确认前置日志失败 log_id=%s type=%s",
                            log_id,
                            type(resolve_exc).__name__,
                        )
            except Exception as exc:
                item_error = exc
                try:
                    conn.execute(
                        "UPDATE organize_log SET legacy_incomplete=1,error=?,updated_at=? WHERE id=?",
                        ("操作明细写入失败，请人工核对", db.now(), int(log_id)),
                    )
                except Exception as mark_exc:
                    # 主日志可能由兼容适配器或测试替身写入；保留原始
                    # 明细异常与 log_id，让外层使用公共更新接口兜底标记。
                    logger.warning(
                        "事务内标记整理审计不完整失败 type=%s",
                        type(mark_exc).__name__,
                    )
        if item_error is not None:
            raise _OrganizeAuditWriteError(log_id, item_error)
        return int(log_id)

    def clean_empty_dirs(
        self,
        source_dir_id: str,
        *,
        with_report: bool = False,
        protected_source_ids: set[str] | None = None,
    ) -> int | dict[str, int]:
        """安全扫描并清理空子目录；扫描不完整时不执行任何删除。"""
        scanned_dirs: list[tuple[str, int, str, int]] = []
        scan_failures = 0
        protected = {str(item) for item in (protected_source_ids or set()) if str(item)}
        protected.add(str(source_dir_id))
        traversal = _TraversalBudget(*self._traversal_limits)

        def scan(dir_id: str, depth: int) -> bool:
            nonlocal scan_failures
            if not traversal.enter(dir_id, depth):
                return True
            try:
                files = self.client.list_dir(dir_id)
                traversal.consume_entries(len(files))
            except _TraversalLimitExceeded:
                raise
            except Exception as exc:
                scan_failures += 1
                logger.warning(
                    "扫描空目录失败 dir_id=%s type=%s",
                    dir_id, type(exc).__name__,
                )
                return False
            for item in files:
                if item.is_dir:
                    child_id = str(item.file_id or "").strip()
                    if not child_id or child_id in protected:
                        continue
                    if not scan(child_id, depth + 1):
                        return False
                    try:
                        updated_at = max(0, int(getattr(item, "updated_at", 0) or 0))
                    except (TypeError, ValueError):
                        updated_at = 0
                    scanned_dirs.append((
                        child_id,
                        depth + 1,
                        str(getattr(item, "etag", "") or ""),
                        updated_at,
                    ))
            return True

        scan_complete = False
        try:
            scan_complete = scan(source_dir_id, 0)
        except _TraversalLimitExceeded as exc:
            logger.warning(
                "空目录扫描触发安全预算 kind=%s limit=%s dirs=%s entries=%s",
                exc.kind, exc.limit, traversal.directories, traversal.entries,
            )
            raise RuntimeError(
                "空目录扫描超过安全上限，未执行清理；请缩小来源范围后重试"
            ) from exc
        if not scan_complete:
            report = {
                "cleaned": 0,
                "delete_failures": 0,
                "scan_failures": max(1, scan_failures),
            }
            if with_report:
                return report
            raise RuntimeError("空目录扫描不完整，未执行清理")
        report = self._clean_empty_dirs_report(
            scanned_dirs,
            protected_source_ids=protected,
        )
        if with_report:
            # 保持公共 clean_empty_dirs() 的历史返回合同稳定；整理主链路需要的
            # 细分保留原因由内部 _clean_empty_dirs_report() 消费，避免 Agent/API
            # 调用方因新增诊断字段发生严格字典比较回归。
            return {
                "cleaned": int(report.get("cleaned", 0) or 0),
                "delete_failures": int(report.get("delete_failures", 0) or 0),
                "scan_failures": scan_failures,
            }
        return report["cleaned"]

    def _clean_empty_dirs(
        self,
        scanned_dirs: list[tuple],
        *,
        protected_source_ids: set[str] | None = None,
    ) -> int:
        """自底向上删除本次扫描到的空子目录，不处理受保护来源根。"""
        return self._clean_empty_dirs_report(
            scanned_dirs, protected_source_ids=protected_source_ids
        )["cleaned"]

    def _clean_empty_dirs_report(
        self,
        scanned_dirs: list[tuple],
        *,
        protected_source_ids: set[str] | None = None,
    ) -> dict:
        """安全清理空目录，并返回不泄露目录标识的结构化保留原因。"""
        cleaned = 0
        delete_failures = 0
        protected_count = 0
        not_empty = 0
        unavailable = 0
        protected = {str(item) for item in (protected_source_ids or set()) if str(item)}
        delete_empty = getattr(self.client, "delete_empty_directory", None)
        explicit_capability = getattr(
            self.client, "supports_guarded_empty_directory_delete", None
        )
        if explicit_capability is None:
            explicit_capability = getattr(
                self.client, "supports_atomic_empty_directory_delete", None
            )
        capability_available = callable(delete_empty) and explicit_capability is True
        unique_dirs: dict[str, tuple[str, int, str, int]] = {}
        for item in scanned_dirs:
            if len(item) < 2:
                continue
            dir_id = str(item[0] or "").strip()
            if not dir_id:
                continue
            try:
                depth = max(0, int(item[1] or 0))
            except (TypeError, ValueError):
                depth = 0
            etag = str(item[2] or "") if len(item) > 2 else ""
            try:
                updated_at = max(0, int(item[3] or 0)) if len(item) > 3 else 0
            except (TypeError, ValueError):
                updated_at = 0
            previous = unique_dirs.get(dir_id)
            if previous is None or depth > previous[1]:
                unique_dirs[dir_id] = (dir_id, depth, etag, updated_at)
        candidates = len(unique_dirs)
        if not capability_available:
            if unique_dirs:
                logger.warning(
                    "空目录清理已跳过：Provider 不支持带复核的回收站删除（候选 %s 个）",
                    len(unique_dirs),
                )
            return {
                "cleaned": 0,
                "delete_failures": 0,
                "unsupported": candidates,
                "candidates": candidates,
                "protected": 0,
                "not_empty": 0,
                "unavailable": 0,
                "reasons": ["云盘接口不支持安全空目录清理"] if candidates else [],
            }

        failure_reasons: dict[str, int] = {}
        for item in sorted(unique_dirs.values(), key=lambda row: row[1], reverse=True):
            dir_id, _depth = item[:2]
            expected_etag = str(item[2] or "") if len(item) > 2 else ""
            try:
                expected_updated_at = max(0, int(item[3] or 0)) if len(item) > 3 else 0
            except (TypeError, ValueError):
                expected_updated_at = 0
            if str(dir_id) in protected:
                protected_count += 1
                continue
            try:
                if self.client.list_dir(dir_id):
                    not_empty += 1
                    continue
                # 移动/改名会更新目录版本；删除前必须刷新快照，不能继续使用
                # 扫描阶段的旧 etag/updated_at，否则空目录会被安全校验保留。
                current = self.client.file_info(dir_id)
                if current is None or not bool(getattr(current, "is_dir", False)):
                    unavailable += 1
                    continue
                expected_etag = str(getattr(current, "etag", "") or "")
                try:
                    expected_updated_at = max(
                        0, int(getattr(current, "updated_at", 0) or 0)
                    )
                except (TypeError, ValueError):
                    expected_updated_at = 0
                if not expected_etag and expected_updated_at <= 0:
                    raise RuntimeError("目录版本信息缺失，已保留")
                execute_recycle_bin_delete(
                    self.client,
                    trigger="empty_dir_cleanup",
                    reason="整理完成后清理本次扫描确认的空目录",
                    candidate=DeleteCandidate(
                        file_id=str(dir_id),
                        name=f"空目录 {dir_id}",
                        parent_id="",
                    ),
                    delete_operation=lambda current_id=str(dir_id), current_etag=expected_etag,
                    current_updated_at=expected_updated_at: delete_empty(
                        current_id,
                        expected_etag=current_etag,
                        expected_updated_at=current_updated_at,
                    ),
                )
                cleaned += 1
                logger.debug("清理空目录: %s", dir_id)
            except Exception as exc:
                delete_failures += 1
                reason = " ".join(str(exc or type(exc).__name__).split())[:160]
                failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
        if failure_reasons:
            summary = "；".join(
                f"{reason}（{count} 个）" for reason, count in failure_reasons.items()
            )
            logger.warning(
                "空目录清理部分失败：共 %s 个；%s",
                delete_failures,
                summary,
            )
        reasons: list[str] = []
        if protected_count:
            reasons.append(f"{protected_count} 个来源或归档根目录按安全策略保留")
        if not_empty:
            reasons.append(
                f"{not_empty} 个目录仍含待确认、跳过、伴随或其他文件"
            )
        if unavailable:
            reasons.append(f"{unavailable} 个目录状态已变化，已安全保留")
        reasons.extend(
            f"{reason}（{count} 个）" for reason, count in failure_reasons.items()
        )
        return {
            "cleaned": cleaned,
            "delete_failures": delete_failures,
            "unsupported": 0,
            "candidates": candidates,
            "protected": protected_count,
            "not_empty": not_empty,
            "unavailable": unavailable,
            "reasons": reasons,
        }

    def _ensure_dir_chain(
        self,
        root_id: str,
        path: str,
        cache: dict[tuple[str, str], str] | None = None,
    ) -> str:
        cur = root_id
        for part in path.split("/"):
            if not part:
                continue
            cache_key = (cur, part)
            sub = cache.get(cache_key) if cache is not None else None
            if not sub:
                sub = self._find_subdir(cur, part)
            if sub:
                if cache is not None:
                    cache[cache_key] = sub
                cur = sub
            else:
                try:
                    new_id = self.client.create_dir(part, cur)
                except Exception as exc:
                    # 目录可能在“查询不存在”与“创建”之间被并发任务或用户建立。
                    # 仅在创建失败后重新读取一次父目录；只复用唯一同名目录，
                    # 重复目录仍由 _find_subdir fail-closed，其他异常重抛原创建错误。
                    try:
                        raced_sub = self._find_subdir(cur, part)
                    except DirectoryScrapeConflictError:
                        raise
                    except Exception:
                        raced_sub = None
                    if raced_sub:
                        if cache is not None:
                            cache[cache_key] = raced_sub
                        cur = raced_sub
                        continue
                    logger.warning(
                        "创建目标目录失败 parent=%s type=%s",
                        cur, type(exc).__name__,
                    )
                    raise
                if not new_id:
                    raise RuntimeError(f"创建目标目录失败: {part}")
                if cache is not None:
                    cache[cache_key] = new_id
                cur = new_id
        return cur

    def _find_subdir(self, parent_id: str, name: str) -> str:
        try:
            entries = self.client.list_dir(parent_id)
        except Exception as exc:
            logger.warning(
                "读取目标子目录失败 parent=%s name=%s type=%s",
                parent_id, name, type(exc).__name__,
            )
            raise
        matches = [
            item for item in entries
            if item.is_dir and item.name == name
        ]
        if len(matches) > 1:
            raise DirectoryScrapeConflictError(
                f"归档目标存在重复同名目录：{name}，请先在网盘中合并或更名"
            )
        return matches[0].file_id if matches else None

    def _find_file(self, dir_id: str, name: str) -> GuangYaFile:
        try:
            for f in self.client.list_dir(dir_id):
                if not f.is_dir and f.name == name:
                    return f
        except Exception as exc:
            logger.warning(
                "读取目标文件失败 dir=%s name=%s type=%s",
                dir_id, name, type(exc).__name__,
            )
            raise
        return None
