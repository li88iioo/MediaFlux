"""识别实验的无隐私影子对比工具。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from app.modules.recognition.models import ReleaseParseEvidence

_FIELDS = ("title", "year", "media_type", "season", "episode")
_CATEGORIES = ("matched", "false_positive", "unresolved", "conflict")


@dataclass(frozen=True)
class ShadowFieldOutcome:
    """只保留 case_id/字段/分类，不携带文件名、路径或标题原文。"""

    case_id: str
    field: str
    baseline_category: str
    experiment_category: str
    tags: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return self.baseline_category != self.experiment_category

    @property
    def regressed(self) -> bool:
        rank = {"matched": 0, "unresolved": 1, "conflict": 2, "false_positive": 3}
        return rank[self.experiment_category] > rank[self.baseline_category]


@dataclass(frozen=True)
class ShadowEvaluationSummary:
    total: int
    unchanged: int
    improved: int
    regressed: int
    new_false_positive: int
    baseline_categories: Mapping[str, int]
    experiment_categories: Mapping[str, int]
    changed_keys: tuple[str, ...]
    new_false_positive_keys: tuple[str, ...]

    @property
    def promotion_allowed(self) -> bool:
        """只有实际改善、无回退且未新增 false-positive 才允许晋级。"""
        return self.improved > 0 and self.regressed == 0 and self.new_false_positive == 0


def _classify(expected: object, actual: object) -> str:
    if actual == expected:
        return "matched"
    if expected in (None, "") and actual not in (None, ""):
        return "false_positive"
    if expected not in (None, "") and actual in (None, ""):
        return "unresolved"
    return "conflict"


def project_anime_evidence(
    evidence: Iterable[ReleaseParseEvidence],
    *,
    minimum_confidence: float = 0.85,
) -> dict[str, object]:
    """把一致的高置信证据投影为候选字段；冲突字段保持未决。"""
    candidates: dict[str, set[object]] = {}
    special = False
    for item in evidence:
        if item.source != "anitomy_ng" or float(item.confidence) < minimum_confidence:
            continue
        if item.kind == "special_type":
            special = True
            continue
        if item.kind not in {"title", "year", "season", "episode"}:
            continue
        candidates.setdefault(item.kind, set()).add(item.value)

    projection: dict[str, object] = {}
    for field, values in candidates.items():
        if len(values) == 1:
            projection[field] = next(iter(values))
    if special and "season" not in projection:
        projection["season"] = 0
    if projection.get("episode") is not None:
        projection["media_type"] = "tv"
    return projection


def merge_shadow_projection(
    baseline: Mapping[str, object],
    evidence: Iterable[ReleaseParseEvidence],
    *,
    minimum_confidence: float = 0.85,
) -> dict[str, object]:
    """构造“若偏好实验解析器”的离线候选；不修改 baseline。"""
    merged = dict(baseline)
    merged.update(project_anime_evidence(evidence, minimum_confidence=minimum_confidence))
    return merged


def compare_shadow_case(
    *,
    case_id: str,
    tags: Sequence[str],
    expected: Mapping[str, object],
    baseline: Mapping[str, object],
    experiment: Mapping[str, object],
    fields: Sequence[str] = _FIELDS,
) -> tuple[ShadowFieldOutcome, ...]:
    return tuple(
        ShadowFieldOutcome(
            case_id=case_id,
            field=field,
            baseline_category=_classify(expected.get(field), baseline.get(field)),
            experiment_category=_classify(expected.get(field), experiment.get(field)),
            tags=tuple(tags),
        )
        for field in fields
    )


def summarize_shadow_evaluation(
    outcomes: Iterable[ShadowFieldOutcome],
) -> ShadowEvaluationSummary:
    rows = tuple(outcomes)
    baseline = {category: 0 for category in _CATEGORIES}
    experiment = {category: 0 for category in _CATEGORIES}
    improved = 0
    regressed = 0
    changed_keys: list[str] = []
    new_fp_keys: list[str] = []
    rank = {"matched": 0, "unresolved": 1, "conflict": 2, "false_positive": 3}

    for row in rows:
        baseline[row.baseline_category] += 1
        experiment[row.experiment_category] += 1
        if not row.changed:
            continue
        key = f"{row.case_id}.{row.field}"
        changed_keys.append(key)
        if rank[row.experiment_category] < rank[row.baseline_category]:
            improved += 1
        else:
            regressed += 1
        if row.experiment_category == "false_positive" and row.baseline_category != "false_positive":
            new_fp_keys.append(key)

    return ShadowEvaluationSummary(
        total=len(rows),
        unchanged=len(rows) - len(changed_keys),
        improved=improved,
        regressed=regressed,
        new_false_positive=len(new_fp_keys),
        baseline_categories=baseline,
        experiment_categories=experiment,
        changed_keys=tuple(changed_keys),
        new_false_positive_keys=tuple(new_fp_keys),
    )
