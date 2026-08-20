"""媒体版本分类与可共存策略。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MediaVariant:
    """可用于整理分桶的三态媒体版本；``None`` 表示元数据不足。"""

    dolby_vision: bool | None = None
    atmos: bool | None = None
    remux: bool | None = None

    def filename_tags(self, rules: Any) -> tuple[str, ...]:
        """按已启用的共存维度返回稳定、可读的文件名标签。"""
        if not bool(getattr(rules, "keep_multi_versions", False)):
            return ()
        tags: list[str] = []
        if self.dolby_vision is not None:
            tags.append("DoVi" if self.dolby_vision else "Standard")
        if self.atmos is not None:
            tags.append("Atmos" if self.atmos else "NonAtmos")
        if bool(getattr(rules, "keep_remux_variant", False)) and self.remux is not None:
            tags.append("Remux" if self.remux else "Encode")
        return tuple(tags)


def _profile_text(profile: Any) -> str:
    if profile is None:
        return ""
    if isinstance(profile, str):
        return profile
    if isinstance(profile, dict):
        values = profile.values()
    else:
        values = (
            getattr(profile, field, "")
            for field in (
                "dynamic_range", "audio_codec", "audio_channels", "source",
                "resolution", "video_codec",
            )
        )
    return " ".join(str(value or "") for value in values)


def classify_variant(name: str, profile: Any = None) -> MediaVariant:
    """只依据明确文件名/探测信息分类，不把缺失信息猜成标准版本。"""
    text = " ".join((str(name or ""), _profile_text(profile)))
    compact = re.sub(r"[^a-z0-9+]", "", text.lower())
    if isinstance(profile, dict):
        probed_dolby_vision = profile.get("dolby_vision")
        probed_atmos = profile.get("atmos")
    else:
        probed_dolby_vision = getattr(profile, "dolby_vision", None)
        probed_atmos = getattr(profile, "atmos", None)

    if probed_dolby_vision is not None:
        dolby_vision: bool | None = bool(probed_dolby_vision)
    elif "dolbyvision" in compact or "dovi" in compact:
        dolby_vision = True
    elif re.search(r"(?:^|[._\s-])(?:standard|sdr|hdr|hdr10\+?|hdr10plus|hlg)(?:[._\s-]|$)", text, re.I):
        dolby_vision = False
    else:
        dolby_vision = None

    if probed_atmos is not None:
        atmos: bool | None = bool(probed_atmos)
    elif re.search(r"(?:^|[._\s-])non[._\s-]?atmos(?:[._\s-]|$)", text, re.I):
        atmos = False
    elif re.search(r"(?:^|[._\s-])atmos(?:[._\s-]|$)", text, re.I):
        atmos = True
    elif re.search(
        r"(?:^|[._\s-])(?:truehd|ddp|eac3|e-?ac-?3|ac-?3|dts(?:-?hd)?|aac|flac)"
        r"(?:[._\s-]?[257][._]1)?(?:[._\s-]|$)",
        text,
        re.I,
    ):
        atmos = False
    else:
        atmos = None

    if re.search(r"(?:^|[._\s-])remux(?:[._\s-]|$)", text, re.I):
        remux: bool | None = True
    elif re.search(
        r"(?:(?:^|[._\s-])encode(?:[._\s-]|$)|web[ ._-]?(?:dl|rip)|hdtv|bluray[ ._-]?(?:rip|encode)|"
        r"(?:^|[._\s-])(?:x26[45]|h[ ._-]?26[45]|hevc|av1)(?:[._\s-]|$))",
        text,
        re.I,
    ):
        remux = False
    else:
        remux = None

    return MediaVariant(dolby_vision=dolby_vision, atmos=atmos, remux=remux)


def variants_can_coexist(existing: MediaVariant, incoming: MediaVariant, rules: Any) -> bool:
    """仅在启用维度存在明确差异时允许绕过同桶替换。"""
    if not bool(getattr(rules, "keep_multi_versions", False)):
        return False
    if (
        existing.dolby_vision is not None
        and incoming.dolby_vision is not None
        and existing.dolby_vision != incoming.dolby_vision
    ):
        return True
    if (
        existing.atmos is not None
        and incoming.atmos is not None
        and existing.atmos != incoming.atmos
    ):
        return True
    return bool(getattr(rules, "keep_remux_variant", False)) and (
        existing.remux is not None
        and incoming.remux is not None
        and existing.remux != incoming.remux
    )
