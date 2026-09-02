"""剧集发布编号到 TMDB 季集编号的保守映射。

发布组常见两类编号与 TMDB 不一致：
1. 把多季连续内容发布为 ``S01E01-E24``；
2. 第二季沿用总集数，如 ``S02E13-E24``，而 TMDB 第二季从 E01 开始。

本模块只在 TMDB 季集数量足以形成唯一、高置信度映射时自动转换；
无法证明的情况保持原编号，继续交给现有 TMDB 越界校验和人工确认链路。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import unicodedata

NUMBERING_MODES = {"auto", "standard", "absolute", "season_continuous"}
NUMBERING_MODE_LABELS = {
    "auto": "自动判断",
    "standard": "保持原编号",
    "absolute": "按绝对集数映射",
    "season_continuous": "按跨季连续编号映射",
    "tmdb_special": "按 TMDB 特别篇映射",
}

# 发布组把动画长篇按 24 集一“季”切分、而 TMDB 合并为单季时，后续季的
# 起始绝对集号通常越过 24 * (season - 1)。这只是自动模式的最低证据门槛；
# 12/13 集分割放送必须继续由带播出日期停播间隔的 cour 映射单独证明。
_AUTO_MERGED_SEASON_SPAN = 24

_EXPLICIT_RANGE_PATTERNS = (
    re.compile(
        r"(?i)S(?P<season>\d{1,2})\s*E(?P<start>\d{1,4})\s*[-~～–—]\s*"
        r"(?:(?:S(?P<end_season>\d{1,2})\s*)?E)?(?P<end>\d{1,4})"
    ),
    re.compile(
        r"(?i)[\[(【]\s*(?P<start>\d{1,4})\s*[-~～–—]\s*(?P<end>\d{1,4})"
        r"(?:\s*(?:FIN|END|全))?\s*[\])】]"
    ),
    # 动画整季包常写成 ``S04 | 01-11``。显式季号、竖线分隔和纯数字范围
    # 三项缺一不可，避免把多语言标题中的普通竖线或年份范围误判成集数。
    re.compile(
        r"(?i)(?:^|[\s._\-])S(?P<season>\d{1,2})\s*[|｜]\s*"
        r"(?P<start>\d{1,4})\s*[-~～–—]\s*(?P<end>\d{1,4})"
        r"(?=$|[\s._\-\[【(（])"
    ),
)

_TMDB_SPECIAL_DECIMAL_MARKER = re.compile(
    r"(?<!\d)(?P<marker>\d{1,4}\.\d{1,2})(?!\d)"
)
_SPECIAL_ASSOCIATION_WINDOW_DAYS = 21


@dataclass(frozen=True)
class DirectoryEpisodeEvidence:
    """同一物理目录内连续剧集形成的映射证据。"""

    directory_key: str
    directory_name: str
    source_season: int
    range_start: int
    range_end: int
    episode_count: int
    contiguous: bool = True
    declared_range_matches: bool = False


@dataclass(frozen=True)
class EpisodeMappingPlan:
    source_season: int | None
    source_episode: int | None
    target_season: int | None
    target_episode: int | None
    mode: str = "standard"
    reason: str = "identity"
    confidence: float = 1.0
    range_start: int | None = None
    range_end: int | None = None

    @property
    def changed(self) -> bool:
        return (
            self.source_season != self.target_season
            or self.source_episode != self.target_episode
        )

    @property
    def label(self) -> str:
        return NUMBERING_MODE_LABELS.get(self.mode, self.mode)

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["changed"] = self.changed
        payload["label"] = self.label
        return payload


def normalize_numbering_mode(value: object) -> str:
    mode = str(value or "auto").strip().lower()
    return mode if mode in NUMBERING_MODES else "auto"


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def season_episode_counts(detail: dict | None) -> dict[int, int]:
    result: dict[int, int] = {}
    seasons = detail.get("seasons") if isinstance(detail, dict) else None
    if not isinstance(seasons, list):
        return result
    for item in seasons:
        if not isinstance(item, dict):
            continue
        season = _positive_int(item.get("season_number"))
        count = _positive_int(item.get("episode_count"))
        if season is None or season <= 0 or count is None or count <= 0:
            continue
        result[season] = count
    return result


def infer_merged_season_cour_mapping(
    *,
    source_season: int | None,
    source_episode: int | None,
    detail: dict | None,
    season_detail: dict | None,
    directory_evidence: DirectoryEpisodeEvidence | None = None,
    minimum_hiatus_days: int = 42,
) -> EpisodeMappingPlan:
    """把发布方分季编号映射到 TMDB 合并季中的绝对位置。

    部分动画发布方把半年番/分割放送写作 ``S02E06``，而 TMDB 仍把两段
    放送合并为单一的 Season 01。仅凭季集数量无法证明正确目标：S02E06
    既不能直接写入不存在的第二季，也不能简单退化为 S01E06。这里要求
    TMDB 只有一个常规季、该季集数与逐集详情完整连续，并以至少 42 天的
    播出间隔作为分割季边界，才把第二段第 6 集映射为合并季中的对应集。

    调用方仍必须对返回目标执行 TMDB 最终季集校验；任何缺失或歧义均保持
    原位置并以零置信度失败关闭。
    """
    season = _positive_int(source_season)
    episode = _positive_int(source_episode)
    identity = EpisodeMappingPlan(
        season, episode, season, episode,
        mode="auto",
        reason="merged_season_cour_not_provable",
        confidence=0.0,
    )
    if season is None or season < 2 or episode is None or episode < 1:
        return identity
    if isinstance(minimum_hiatus_days, bool) or minimum_hiatus_days < 1:
        return identity

    counts = season_episode_counts(detail)
    if len(counts) != 1:
        return identity
    target_season, declared_count = next(iter(counts.items()))
    if declared_count < 1 or not isinstance(season_detail, dict):
        return identity
    season_number = _positive_int(season_detail.get("season_number"))
    if season_number != target_season:
        return identity
    raw_episodes = season_detail.get("episodes")
    if not isinstance(raw_episodes, list) or len(raw_episodes) != declared_count:
        return identity

    aired: list[tuple[int, date]] = []
    seen: set[int] = set()
    for item in raw_episodes:
        if not isinstance(item, dict):
            return identity
        number = _positive_int(item.get("episode_number"))
        raw_air_date = str(item.get("air_date") or "").strip()
        if number is None or number < 1 or number in seen or not raw_air_date:
            return identity
        try:
            aired_on = date.fromisoformat(raw_air_date)
        except ValueError:
            return identity
        seen.add(number)
        aired.append((number, aired_on))
    aired.sort(key=lambda item: item[0])
    if [number for number, _aired_on in aired] != list(range(1, declared_count + 1)):
        return identity

    segments: list[tuple[int, int]] = []
    segment_start = 1
    for index in range(1, len(aired)):
        previous_number, previous_date = aired[index - 1]
        current_number, current_date = aired[index]
        # TMDB/兼容数据源偶尔会把同批上线的连续集写成相同播出日。
        # 同日不破坏顺序，也不能成为分割季边界；只有日期倒退才说明
        # 逐集时间线不可信并失败关闭。
        if current_date < previous_date:
            return identity
        if (current_date - previous_date).days >= minimum_hiatus_days:
            segments.append((segment_start, previous_number))
            segment_start = current_number
    segments.append((segment_start, declared_count))

    # 只有“发布季号 == 可证明的停播分段总数”时，第 N 季与第 N 段才是
    # 唯一对应。若 TMDB 单季里还有更多分段（例如前面的发布季本身也分
    # cour），机械取 ``segments[season - 1]`` 会把后续季映射到更早分段。
    if len(segments) != season:
        return identity
    range_start, range_end = segments[season - 1]

    evidence = directory_evidence
    absolute_segment_proven = bool(
        evidence is not None
        and evidence.contiguous
        and evidence.source_season == season
        and evidence.range_start == range_start
        and evidence.range_end == range_end
        and evidence.episode_count == range_end - range_start + 1
        and range_start <= episode <= range_end
    )
    if absolute_segment_proven:
        target_episode = episode
        reason = "publisher_absolute_cour_mapped_to_merged_tmdb_season"
    elif evidence is not None and evidence.range_start > 1:
        # 目录从大于 1 的集号开始时，它表达的是绝对编号候选；若没有完整
        # 覆盖目标分段，不能再把同一个数字二次解释成“季内第 N 集”。
        return identity
    else:
        target_episode = range_start + episode - 1
        reason = "publisher_cour_mapped_to_merged_tmdb_season"
    if target_episode > range_end:
        return identity
    return EpisodeMappingPlan(
        season, episode, target_season, target_episode,
        mode="absolute",
        reason=reason,
        confidence=1.0,
        range_start=range_start,
        range_end=range_end,
    )


def extract_release_episode_range(text: str) -> tuple[int | None, int | None, int | None]:
    """返回 ``(显式季号, 起始集, 结束集)``；仅接受明确范围语法。"""
    value = str(text or "")
    for pattern in _EXPLICIT_RANGE_PATTERNS:
        match = pattern.search(value)
        if not match:
            continue
        groups = match.groupdict()
        start = _positive_int(groups.get("start"))
        end = _positive_int(groups.get("end"))
        season = _positive_int(groups.get("season"))
        end_season = _positive_int(groups.get("end_season"))
        # 跨季范围不能压扁成单季范围，否则 ``S01E11-S02E03`` 会把
        # 第二季集号错误解释成第一季 E03。等待更高层按目录证据处理。
        if end_season is not None and season is not None and end_season != season:
            continue
        if start is None or end is None or start < 1 or end < start:
            continue
        return season, start, end
    return None, None, None


def build_directory_episode_evidence(
    entries: list[tuple[str, str, int | None, int | None]],
    *,
    minimum_episodes: int = 3,
) -> dict[str, DirectoryEpisodeEvidence]:
    """按物理目录聚合连续剧集证据。

    默认至少需要 3 个连续编号，避免单个 ``S02E13`` 被擅自重解释。
    调用方可为更窄的、另有强约束的推断读取短序列；普通季集映射仍在
    ``infer_episode_mapping`` 内坚持至少 3 集的门槛。
    """
    if isinstance(minimum_episodes, bool):
        minimum_episodes = 3
    try:
        minimum_count = max(1, int(minimum_episodes))
    except (TypeError, ValueError):
        minimum_count = 3
    grouped: dict[str, list[tuple[str, int | None, int]]] = {}
    for directory_key, directory_name, season, episode in entries:
        parsed_season = _positive_int(season)
        parsed_episode = _positive_int(episode)
        if parsed_episode is None or parsed_episode <= 0:
            continue
        # season=None 只在后续“同目录、从 1 开始、至少 3 集且严格连续”
        # 的证据门禁内解释为发布方的绝对集号；S00 仍是 Specials，不能参与。
        if parsed_season is not None and parsed_season <= 0:
            continue
        grouped.setdefault(str(directory_key), []).append(
            (str(directory_name or directory_key), parsed_season, parsed_episode)
        )
    result: dict[str, DirectoryEpisodeEvidence] = {}
    for directory_key, rows in grouped.items():
        seasons = {row[1] for row in rows}
        episodes = sorted({row[2] for row in rows})
        if len(episodes) < minimum_count:
            continue
        contiguous = episodes == list(range(episodes[0], episodes[-1] + 1))
        if not contiguous:
            continue
        if seasons == {None}:
            # 裸集号只能在完整序列从 E01 开始时解释成 S01 absolute 包。
            # E13-E24、缺首集或显式/隐式季号混用都继续失败关闭。
            if episodes[0] != 1:
                continue
            source_season = 1
        elif len(seasons) == 1:
            source_season = next(iter(seasons))
            if source_season is None:
                continue
        else:
            continue
        directory_name = rows[0][0]
        declared_season, declared_start, declared_end = extract_release_episode_range(
            directory_name
        )
        declared_matches = bool(
            declared_start == episodes[0]
            and declared_end == episodes[-1]
            and declared_season in (None, source_season)
        )
        result[directory_key] = DirectoryEpisodeEvidence(
            directory_key=directory_key,
            directory_name=directory_name,
            source_season=source_season,
            range_start=episodes[0],
            range_end=episodes[-1],
            episode_count=len(episodes),
            contiguous=True,
            declared_range_matches=declared_matches,
        )
    return result


def _strict_date(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _tmdb_special_decimal_rows(
    season_detail: dict | None,
) -> list[tuple[Decimal, int, date | None]]:
    """提取 TMDB 特别篇标题中的绝对小数集号，歧义数据直接忽略。"""
    if not isinstance(season_detail, dict):
        return []
    season_number = _positive_int(season_detail.get("season_number"))
    if season_number not in (None, 0):
        return []
    episodes = season_detail.get("episodes")
    if not isinstance(episodes, list):
        return []

    rows: list[tuple[Decimal, int, date | None]] = []
    seen: set[tuple[Decimal, int]] = set()
    for item in episodes:
        if not isinstance(item, dict):
            continue
        episode_number = _positive_int(item.get("episode_number"))
        if episode_number is None or episode_number < 1:
            continue
        names = (
            str(item.get("name") or ""),
            str(item.get("original_name") or ""),
        )
        markers: set[Decimal] = set()
        for name in names:
            normalized = unicodedata.normalize("NFKC", name)
            for match in _TMDB_SPECIAL_DECIMAL_MARKER.finditer(normalized):
                try:
                    marker = Decimal(match.group("marker"))
                except InvalidOperation:
                    continue
                if marker > 0:
                    markers.add(marker)
        # 一个特别篇标题若同时携带两个不同小数位置，无法证明该用哪一个。
        if len(markers) != 1:
            continue
        marker = next(iter(markers))
        key = (marker, episode_number)
        if key in seen:
            continue
        seen.add(key)
        rows.append((marker, episode_number, _strict_date(item.get("air_date"))))
    return rows


def match_fractional_tmdb_special(
    source_marker: object,
    season_detail: dict | None,
) -> int | None:
    """把发布名中的 ``48.5`` 精确映射到 TMDB Season 00 集号。

    只有 TMDB 特别篇标题中恰好存在一个同值小数标记时才返回，避免继续用
    “本目录第几个特别篇”冒充 TMDB 的全剧特别篇编号。
    """
    try:
        marker = Decimal(str(source_marker))
    except (InvalidOperation, TypeError, ValueError):
        return None
    matches = {
        episode_number
        for current_marker, episode_number, _air_date
        in _tmdb_special_decimal_rows(season_detail)
        if current_marker == marker
    }
    return next(iter(matches)) if len(matches) == 1 else None


def infer_overflow_tmdb_special_mapping(
    *,
    source_season: int | None,
    source_episode: int | None,
    detail: dict | None,
    source_season_detail: dict | None,
    special_season_detail: dict | None,
    directory_evidence: DirectoryEpisodeEvidence | None,
    directory_member_count: int = 0,
) -> EpisodeMappingPlan | None:
    """把完整季尾部追加编号映射到该播出季关联的 TMDB 特别篇。

    一些发布组会把两段 recap 追加成 ``S02E25/E26``，而 TMDB 把它们记录为
    Season 00。这里同时要求：

    * TMDB 正片季存在且逐集日期完整；
    * 特别篇标题带绝对小数标记（如 24.9、36.5）；
    * 特别篇播出日在该季首尾 21 天窗口内；
    * 当前物理目录恰好是完整正片加尾项，或只剩完整尾项；
    * 目录媒体数、连续编号数和候选特别篇数完全一致。

    任一条件不满足即返回 ``None``，继续由既有越界校验转人工确认。
    """
    season = _positive_int(source_season)
    episode = _positive_int(source_episode)
    if season is None or season < 1 or episode is None or episode < 1:
        return None
    counts = season_episode_counts(detail)
    regular_count = counts.get(season)
    if regular_count is None or episode <= regular_count:
        return None
    evidence = directory_evidence
    if (
        evidence is None
        or not evidence.contiguous
        or evidence.source_season != season
        or evidence.episode_count < 1
        or evidence.range_end - evidence.range_start + 1 != evidence.episode_count
        or not evidence.range_start <= episode <= evidence.range_end
        or int(directory_member_count or 0) != evidence.episode_count
    ):
        return None

    if not isinstance(source_season_detail, dict):
        return None
    season_number = _positive_int(source_season_detail.get("season_number"))
    if season_number not in (None, season):
        return None
    raw_regular_episodes = source_season_detail.get("episodes")
    if not isinstance(raw_regular_episodes, list) or len(raw_regular_episodes) != regular_count:
        return None
    regular_dates: list[date] = []
    regular_numbers: set[int] = set()
    for item in raw_regular_episodes:
        if not isinstance(item, dict):
            return None
        number = _positive_int(item.get("episode_number"))
        aired_on = _strict_date(item.get("air_date"))
        if number is None or number < 1 or aired_on is None:
            return None
        regular_numbers.add(number)
        regular_dates.append(aired_on)
    if regular_numbers != set(range(1, regular_count + 1)):
        return None

    first_air = min(regular_dates)
    last_air = max(regular_dates)
    previous_total = sum(count for number, count in counts.items() if number < season)
    marker_floor = Decimal(previous_total)
    marker_ceiling = Decimal(previous_total + regular_count + 1)
    dated_candidates: list[tuple[Decimal, int]] = []
    seen_episodes: set[int] = set()
    for marker, episode_number, aired_on in _tmdb_special_decimal_rows(
        special_season_detail
    ):
        if aired_on is None:
            continue
        if not marker_floor < marker < marker_ceiling:
            continue
        if not (
            abs((aired_on - first_air).days) <= _SPECIAL_ASSOCIATION_WINDOW_DAYS
            or first_air <= aired_on <= last_air
            or abs((aired_on - last_air).days) <= _SPECIAL_ASSOCIATION_WINDOW_DAYS
        ):
            continue
        if episode_number in seen_episodes:
            return None
        seen_episodes.add(episode_number)
        dated_candidates.append((marker, episode_number))
    dated_candidates.sort()
    if not dated_candidates:
        return None

    overflow_count = len(dated_candidates)
    expected_end = regular_count + overflow_count
    complete_pack = bool(
        evidence.range_start == 1
        and evidence.range_end == expected_end
        and evidence.episode_count == expected_end
    )
    complete_tail = bool(
        evidence.range_start == regular_count + 1
        and evidence.range_end == expected_end
        and evidence.episode_count == overflow_count
    )
    if not (complete_pack or complete_tail):
        return None

    offset = episode - regular_count - 1
    if not 0 <= offset < overflow_count:
        return None
    target_episode = dated_candidates[offset][1]
    return EpisodeMappingPlan(
        season,
        episode,
        0,
        target_episode,
        mode="tmdb_special",
        reason="season_overflow_mapped_to_tmdb_special",
        confidence=1.0,
        range_start=evidence.range_start,
        range_end=evidence.range_end,
    )


def _absolute_target(episode: int, counts: dict[int, int]) -> tuple[int, int] | None:
    remaining = int(episode)
    for season in sorted(counts):
        count = counts[season]
        if remaining <= count:
            return season, remaining
        remaining -= count
    return None


def infer_episode_mapping(
    *,
    source_season: int | None,
    source_episode: int | None,
    parent_path: str = "",
    detail: dict | None = None,
    mode: str = "auto",
    directory_evidence: DirectoryEpisodeEvidence | None = None,
) -> EpisodeMappingPlan:
    """生成一次保守映射。

    ``auto`` 只转换原位置在 TMDB 中越界、且季数统计能唯一解释的编号。
    手动模式可显式选择 ``absolute`` 或 ``season_continuous``。
    """
    normalized_mode = normalize_numbering_mode(mode)
    season = _positive_int(source_season)
    episode = _positive_int(source_episode)
    range_season, range_start, range_end = extract_release_episode_range(parent_path)
    identity = EpisodeMappingPlan(
        season, episode, season, episode,
        mode="standard" if normalized_mode == "standard" else normalized_mode,
        reason="identity",
        confidence=1.0,
        range_start=range_start,
        range_end=range_end,
    )
    if season is None or season == 0 or episode is None or episode < 1:
        return identity

    counts = season_episode_counts(detail)
    if not counts:
        return EpisodeMappingPlan(
            season, episode, season, episode,
            mode=normalized_mode,
            reason="tmdb_season_counts_missing",
            confidence=0.0,
            range_start=range_start,
            range_end=range_end,
        )

    if normalized_mode == "standard":
        return identity

    evidence = directory_evidence
    if (
        evidence is None
        and range_start is not None
        and range_end is not None
        and range_end - range_start + 1 >= 3
        and range_season in (None, season)
    ):
        # 明确写在目录/发布名中的连续范围本身就是目录级证据；这保留了
        # 旧调用方的兼容性，同时仍不会改写孤立的普通 SxxExx 文件。
        evidence = DirectoryEpisodeEvidence(
            directory_key=str(parent_path or "__range__"),
            directory_name=str(parent_path or ""),
            source_season=season,
            range_start=range_start,
            range_end=range_end,
            episode_count=range_end - range_start + 1,
            contiguous=True,
            declared_range_matches=True,
        )
    evidence_valid = bool(
        evidence
        and evidence.contiguous
        and evidence.episode_count >= 3
        and evidence.source_season == season
        and evidence.range_start <= episode <= evidence.range_end
    )
    current_count = counts.get(season)
    previous_total = sum(count for number, count in counts.items() if number < season)

    # 自动模式必须有同目录连续集数证据。手动模式仍允许显式转换。
    if normalized_mode == "auto" and not evidence_valid:
        if current_count is None:
            reason = "tmdb_season_counts_missing"
        elif episode <= current_count:
            return identity
        else:
            reason = "directory_episode_evidence_missing"
        return EpisodeMappingPlan(
            season, episode, season, episode,
            mode=normalized_mode,
            reason=reason,
            confidence=0.0,
            range_start=range_start,
            range_end=range_end,
        )

    if normalized_mode in {"auto", "season_continuous"} and season >= 2 and previous_total:
        target_episode = episode - previous_total
        # 自动跨季连续编号必须从“前面各季总集数之后”开始。
        # 例如四季各 24 集时，S03E49-E72 可唯一换算为 S03E01-E24；
        # 但 S02E01-E26 明确从 E01 重置，E25/E26 只能视为越界异常，
        # 不能因为整包末集超界就错误回卷成 S02E01/E02。
        full_target_season_covered = bool(
            evidence_valid
            and current_count is not None
            and evidence.range_start - previous_total == 1
            and evidence.range_end - previous_total == current_count
            and evidence.episode_count == current_count
        )
        continued_range_proven = bool(
            evidence_valid
            and current_count is not None
            and evidence.range_start > previous_total
            and evidence.range_end > current_count
            and (
                evidence.range_start > current_count
                or full_target_season_covered
            )
        )
        manual_mode = normalized_mode == "season_continuous"
        if (
            current_count is not None
            and 1 <= target_episode <= current_count
            and (manual_mode or continued_range_proven)
        ):
            return EpisodeMappingPlan(
                season, episode, season, target_episode,
                mode="season_continuous",
                reason="continued_numbering_rebased_to_tmdb_season",
                confidence=1.0 if evidence_valid else 0.95,
                range_start=evidence.range_start if evidence_valid else range_start,
                range_end=evidence.range_end if evidence_valid else range_end,
            )
        if manual_mode:
            return EpisodeMappingPlan(
                season, episode, season, episode,
                mode=normalized_mode,
                reason="continued_numbering_not_provable",
                confidence=0.0,
                range_start=range_start,
                range_end=range_end,
            )

    if normalized_mode in {"auto", "absolute"}:
        target = _absolute_target(episode, counts)
        source_position_missing = current_count is None or episode > current_count
        # 显式 S02/S03/S04 且 TMDB 中确有该季时，自动模式不能再把越界
        # 集号跨回前一季。发布季在 TMDB 中不存在时也不能只凭“起点大于 1”
        # 就把 S03E10 误投到合并季 E10；只有 TMDB 恰为单一正片季，且目录
        # 起点已越过前面每个发布季至少 24 集的保守边界，才接受长篇动画的
        # 绝对集号解释。12/13 集分割放送由带停播日期证据的 cour 映射处理。
        merged_single_season_absolute = bool(
            current_count is None
            and len(counts) == 1
            and evidence is not None
            and evidence.range_start
            > _AUTO_MERGED_SEASON_SPAN * (season - 1)
        )
        auto_absolute_allowed = bool(
            evidence_valid
            and source_position_missing
            and (season == 1 or merged_single_season_absolute)
        )
        if (
            target is not None
            and target != (season, episode)
            and (normalized_mode == "absolute" or auto_absolute_allowed)
        ):
            target_season, target_episode = target
            return EpisodeMappingPlan(
                season, episode, target_season, target_episode,
                mode="absolute",
                reason="absolute_numbering_rolled_over_tmdb_seasons",
                confidence=1.0 if evidence_valid else 0.95,
                range_start=evidence.range_start if evidence_valid else range_start,
                range_end=evidence.range_end if evidence_valid else range_end,
            )

    if normalized_mode == "auto" and current_count is not None and episode <= current_count:
        return identity
    return EpisodeMappingPlan(
        season, episode, season, episode,
        mode=normalized_mode,
        reason="mapping_not_provable",
        confidence=0.0,
        range_start=range_start,
        range_end=range_end,
    )
