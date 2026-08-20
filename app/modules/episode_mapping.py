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

NUMBERING_MODES = {"auto", "standard", "absolute", "season_continuous"}
NUMBERING_MODE_LABELS = {
    "auto": "自动判断",
    "standard": "保持原编号",
    "absolute": "按绝对集数映射",
    "season_continuous": "按跨季连续编号映射",
}

_EXPLICIT_RANGE_PATTERNS = (
    re.compile(
        r"(?i)S(?P<season>\d{1,2})\s*E(?P<start>\d{1,4})\s*[-~～–—]\s*"
        r"(?:(?:S(?P<end_season>\d{1,2})\s*)?E)?(?P<end>\d{1,4})"
    ),
    re.compile(
        r"(?i)[\[(【]\s*(?P<start>\d{1,4})\s*[-~～–—]\s*(?P<end>\d{1,4})"
        r"(?:\s*(?:FIN|END|全))?\s*[\])】]"
    ),
)


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

    # 发布季号按第几段放送解释；没有足够强的间隔或目标段不存在时不映射。
    if len(segments) < season:
        return identity
    range_start, range_end = segments[season - 1]
    target_episode = range_start + episode - 1
    if target_episode > range_end:
        return identity
    return EpisodeMappingPlan(
        season, episode, target_season, target_episode,
        mode="absolute",
        reason="publisher_cour_mapped_to_merged_tmdb_season",
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
) -> dict[str, DirectoryEpisodeEvidence]:
    """按物理目录聚合连续剧集证据。

    自动映射至少需要 3 个连续编号，避免单个 ``S02E13`` 被擅自重解释。
    """
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
        if len(episodes) < 3:
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
        pack_exceeds_tmdb_season = bool(
            evidence_valid and current_count is not None and evidence.range_end > current_count
        )
        manual_mode = normalized_mode == "season_continuous"
        if (
            current_count is not None
            and 1 <= target_episode <= current_count
            and (manual_mode or pack_exceeds_tmdb_season)
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
        if (
            target is not None
            and target != (season, episode)
            and (normalized_mode == "absolute" or (evidence_valid and source_position_missing))
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
