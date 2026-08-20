"""自动媒体识别的安全预设与统一门槛。"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import get


AUTOMATIC_MATCH_PRESET_CONFIG_KEY = "GY_ORGANIZE_AUTOMATIC_MATCH_PRESET"
DEFAULT_AUTOMATIC_MATCH_PRESET = "balanced"


@dataclass(frozen=True, slots=True)
class AutomaticMatchPolicy:
    name: str
    threshold: float
    label: str


AUTOMATIC_MATCH_POLICIES: dict[str, AutomaticMatchPolicy] = {
    "conservative": AutomaticMatchPolicy("conservative", 0.93, "保守"),
    "balanced": AutomaticMatchPolicy("balanced", 0.90, "均衡"),
    "aggressive": AutomaticMatchPolicy("aggressive", 0.86, "积极"),
}


def normalize_automatic_match_preset(value: object) -> str:
    preset = str(value or "").strip().lower()
    return preset if preset in AUTOMATIC_MATCH_POLICIES else DEFAULT_AUTOMATIC_MATCH_PRESET


def automatic_match_policy(value: object | None = None) -> AutomaticMatchPolicy:
    raw = get(AUTOMATIC_MATCH_PRESET_CONFIG_KEY, DEFAULT_AUTOMATIC_MATCH_PRESET) \
        if value is None else value
    return AUTOMATIC_MATCH_POLICIES[normalize_automatic_match_preset(raw)]


def automatic_match_threshold(value: object | None = None) -> float:
    return automatic_match_policy(value).threshold


def automatic_match_confirmation_message(value: object | None = None) -> str:
    policy = automatic_match_policy(value)
    percentage = int(round(policy.threshold * 100))
    return (
        f"自动整理采用{policy.label}安全预设，要求严格匹配达到 "
        f"{percentage}%，已转人工确认"
    )
