"""字幕文件身份解析与目录级安全归属规划。"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any


_LANGUAGE_ALIASES = {
    "chs": "zh-Hans",
    "sc": "zh-Hans",
    "zh-cn": "zh-Hans",
    "zh-hans": "zh-Hans",
    "cht": "zh-Hant",
    "tc": "zh-Hant",
    "zh-tw": "zh-Hant",
    "zh-hant": "zh-Hant",
    "zh": "zh",
    "chi": "zh",
    "zho": "zh",
    "en": "en",
    "eng": "en",
    "ja": "ja",
    "jpn": "ja",
    "ko": "ko",
    "kor": "ko",
}
_MARKERS = {"forced", "default"}


@dataclass(frozen=True)
class SubtitleIdentity:
    media_stem: str
    language: str = ""
    forced: bool = False
    default: bool = False
    extension: str = ""

    @classmethod
    def parse(cls, filename: str) -> "SubtitleIdentity":
        raw = str(filename or "").strip()
        if "." in raw:
            stem, extension = raw.rsplit(".", 1)
        else:
            stem, extension = raw, ""
        parts = stem.split(".")
        language = ""
        forced = False
        default = False
        while len(parts) > 1:
            token = parts[-1].strip().lower()
            if token in _LANGUAGE_ALIASES:
                language = _LANGUAGE_ALIASES[token]
                parts.pop()
            elif token in _MARKERS:
                forced = forced or token == "forced"
                default = default or token == "default"
                parts.pop()
            else:
                break
        return cls(
            media_stem=".".join(parts),
            language=language,
            forced=forced,
            default=default,
            extension=extension.lower(),
        )

    @property
    def normalized_suffix(self) -> str:
        parts = [self.language] if self.language else []
        if self.forced:
            parts.append("forced")
        if self.default:
            parts.append("default")
        if self.extension:
            parts.append(self.extension)
        return ".".join(parts)

    def target_name(self, video_target_name: str) -> str:
        video_name = str(video_target_name or "")
        video_stem = video_name.rsplit(".", 1)[0] if "." in video_name else video_name
        suffix = self.normalized_suffix
        return f"{video_stem}.{suffix}" if suffix else video_stem


@dataclass(frozen=True)
class SubtitleCompanionPlan:
    file: Any
    video_file_id: str
    identity: SubtitleIdentity

    def target_name(self, video_target_name: str) -> str:
        return self.identity.target_name(video_target_name)


@dataclass(frozen=True)
class SubtitleSkip:
    file: Any
    reason_code: str
    reason: str


@dataclass(frozen=True)
class SubtitlePlanResult:
    plans: list[SubtitleCompanionPlan]
    skipped: list[SubtitleSkip]


def _stem(name: str) -> str:
    value = str(name or "")
    return value.rsplit(".", 1)[0] if "." in value else value


def plan_subtitle_companions(videos: list[Any], subtitles: list[Any]) -> SubtitlePlanResult:
    """仅为能唯一映射到视频 stem 的字幕生成移动计划。"""
    videos_by_stem: dict[str, list[Any]] = defaultdict(list)
    for video in videos:
        videos_by_stem[_stem(video.name).casefold()].append(video)

    candidates: list[SubtitleCompanionPlan] = []
    skipped: list[SubtitleSkip] = []
    for subtitle in subtitles:
        identity = SubtitleIdentity.parse(subtitle.name)
        matches = videos_by_stem.get(_stem(subtitle.name).casefold(), [])
        if not matches:
            matches = videos_by_stem.get(identity.media_stem.casefold(), [])
        if not matches:
            skipped.append(SubtitleSkip(subtitle, "unmatched", "字幕未唯一匹配任何视频"))
            continue
        if len(matches) != 1:
            skipped.append(SubtitleSkip(subtitle, "ambiguous-video", "多个视频具有相同 stem"))
            continue
        candidates.append(SubtitleCompanionPlan(
            file=subtitle,
            video_file_id=str(matches[0].file_id),
            identity=identity,
        ))

    target_groups: dict[tuple[str, str], list[SubtitleCompanionPlan]] = defaultdict(list)
    for candidate in candidates:
        target_groups[(candidate.video_file_id, candidate.identity.normalized_suffix.casefold())].append(candidate)

    plans: list[SubtitleCompanionPlan] = []
    for group in target_groups.values():
        if len(group) == 1:
            plans.extend(group)
            continue
        for candidate in group:
            skipped.append(SubtitleSkip(
                candidate.file,
                "duplicate-target",
                "多个字幕归一化后目标名称重复",
            ))

    plans.sort(key=lambda item: str(item.file.file_id))
    skipped.sort(key=lambda item: str(item.file.file_id))
    return SubtitlePlanResult(plans=plans, skipped=skipped)
