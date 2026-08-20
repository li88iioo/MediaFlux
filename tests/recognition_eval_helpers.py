"""识别黄金语料的严格加载、字段级分类与报告工具。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

FIELDS = ("title", "year", "media_type", "season", "episode")
CATEGORIES = ("matched", "false_positive", "unresolved", "conflict")
_ALLOWED_TOP_LEVEL_KEYS = {
    "case_id",
    "filename",
    "parent_path",
    "expected",
    "tags",
    "expected_confidence",
    "expected_resolution",
    "assert_fields",
    "notes",
}
_ALLOWED_RESOLUTIONS = {"matched", "unresolved", "conflict"}
_CASE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True)
class ReleaseRecognitionCase:
    case_id: str
    filename: str
    parent_path: str
    expected: dict[str, object]
    tags: tuple[str, ...] = ()
    expected_confidence: float | None = None
    expected_resolution: str | None = None
    assert_fields: tuple[str, ...] = FIELDS
    notes: str = ""


@dataclass(frozen=True)
class FieldOutcome:
    case_id: str
    field: str
    category: str
    expected: object
    actual: object


def _schema_error(case_label: str, message: str) -> ValueError:
    return ValueError(f"{case_label}: {message}")


def _validate_position(case_label: str, field: str, value: object) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise _schema_error(case_label, f"expected.{field} 必须是整数或 null")
    minimum = 0 if field == "season" else 1
    if value < minimum:
        raise _schema_error(case_label, f"expected.{field} 必须 >= {minimum}")


def validate_release_recognition_rows(
    rows: Iterable[Mapping[str, object]],
) -> list[ReleaseRecognitionCase]:
    cases: list[ReleaseRecognitionCase] = []
    seen_case_ids: set[str] = set()
    seen_sources: set[tuple[str, str]] = set()
    for index, raw_row in enumerate(rows, start=1):
        if not isinstance(raw_row, Mapping):
            raise _schema_error(f"第 {index} 行", "样本必须是 JSON object")
        row = dict(raw_row)
        case_label = str(row.get("case_id") or f"第 {index} 行")
        unknown = sorted(set(row) - _ALLOWED_TOP_LEVEL_KEYS)
        if unknown:
            raise _schema_error(case_label, f"未知字段: {', '.join(unknown)}")
        missing = sorted({"case_id", "filename", "parent_path", "expected"} - set(row))
        if missing:
            raise _schema_error(case_label, f"缺少字段: {', '.join(missing)}")

        case_id = row["case_id"]
        if not isinstance(case_id, str) or not _CASE_ID_RE.fullmatch(case_id):
            raise _schema_error(case_label, "case_id 必须是小写字母/数字/连字符组成的 slug")
        if case_id in seen_case_ids:
            raise _schema_error(case_id, "case_id 重复")
        seen_case_ids.add(case_id)

        filename = row["filename"]
        parent_path = row["parent_path"]
        if not isinstance(filename, str) or not filename.strip():
            raise _schema_error(case_id, "filename 必须是非空字符串")
        if not isinstance(parent_path, str):
            raise _schema_error(case_id, "parent_path 必须是字符串")
        source_key = (filename, parent_path)
        if source_key in seen_sources:
            raise _schema_error(case_id, "filename 与 parent_path 组合重复")
        seen_sources.add(source_key)

        expected_raw = row["expected"]
        if not isinstance(expected_raw, Mapping):
            raise _schema_error(case_id, "expected 必须是 object")
        expected = dict(expected_raw)
        unknown_expected = sorted(set(expected) - set(FIELDS))
        missing_expected = sorted(set(FIELDS) - set(expected))
        if unknown_expected:
            raise _schema_error(case_id, f"expected 未知字段: {', '.join(unknown_expected)}")
        if missing_expected:
            raise _schema_error(case_id, f"expected 缺少字段: {', '.join(missing_expected)}")

        title = expected["title"]
        year = expected["year"]
        media_type = expected["media_type"]
        if not isinstance(title, str) or not title.strip() or title != title.strip():
            raise _schema_error(case_id, "expected.title 必须是首尾无空白的非空字符串")
        if not isinstance(year, str) or (year and not re.fullmatch(r"\d{4}", year)):
            raise _schema_error(case_id, "expected.year 必须为空字符串或四位年份")
        if not isinstance(media_type, str) or media_type not in {"movie", "tv"}:
            raise _schema_error(case_id, "expected.media_type 必须是 movie 或 tv")
        _validate_position(case_id, "season", expected["season"])
        _validate_position(case_id, "episode", expected["episode"])
        if media_type == "movie" and expected["episode"] is not None:
            raise _schema_error(case_id, "movie 样本的 expected.episode 必须为 null")

        tags_raw = row.get("tags", [])
        if not isinstance(tags_raw, list) or any(
            not isinstance(tag, str) or not tag.strip() for tag in tags_raw
        ):
            raise _schema_error(case_id, "tags 必须是非空字符串数组")
        tags = tuple(tag.strip() for tag in tags_raw)
        if len(tags) != len(set(tags)):
            raise _schema_error(case_id, "tags 不得重复")

        confidence_raw = row.get("expected_confidence")
        expected_confidence: float | None = None
        if confidence_raw is not None:
            if isinstance(confidence_raw, bool) or not isinstance(confidence_raw, (int, float)):
                raise _schema_error(case_id, "expected_confidence 必须是 0..1 数字")
            expected_confidence = float(confidence_raw)
            if not 0.0 <= expected_confidence <= 1.0:
                raise _schema_error(case_id, "expected_confidence 必须位于 0..1")

        expected_resolution = row.get("expected_resolution")
        if (
            expected_resolution is not None
            and (
                not isinstance(expected_resolution, str)
                or expected_resolution not in _ALLOWED_RESOLUTIONS
            )
        ):
            raise _schema_error(
                case_id,
                "expected_resolution 必须是 matched、unresolved 或 conflict",
            )
        assert_fields_raw = row.get("assert_fields", list(FIELDS))
        if (
            not isinstance(assert_fields_raw, list)
            or not assert_fields_raw
            or any(field not in FIELDS for field in assert_fields_raw)
        ):
            raise _schema_error(
                case_id, "assert_fields 必须是非空的受支持字段数组"
            )
        assert_fields = tuple(str(field) for field in assert_fields_raw)
        if len(assert_fields) != len(set(assert_fields)):
            raise _schema_error(case_id, "assert_fields 不得重复")

        notes = row.get("notes", "")
        if not isinstance(notes, str):
            raise _schema_error(case_id, "notes 必须是字符串")

        cases.append(ReleaseRecognitionCase(
            case_id=case_id,
            filename=filename,
            parent_path=parent_path,
            expected=expected,
            tags=tags,
            expected_confidence=expected_confidence,
            expected_resolution=expected_resolution,
            assert_fields=assert_fields,
            notes=notes,
        ))
    return cases


def load_release_recognition_cases(path: Path) -> list[ReleaseRecognitionCase]:
    rows: list[Mapping[str, object]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"第 {line_number} 行不是有效 JSON: {exc.msg}") from exc
        if not isinstance(payload, Mapping):
            raise ValueError(f"第 {line_number} 行必须是 JSON object")
        rows.append(payload)
    return validate_release_recognition_rows(rows)


def _is_absent(field: str, value: object) -> bool:
    if field in {"title", "year", "media_type"}:
        return value is None or value == ""
    return value is None


def classify_field(field: str, expected: object, actual: object) -> str:
    if field not in FIELDS:
        raise ValueError(f"未知识别字段: {field}")
    if actual == expected:
        return "matched"
    expected_absent = _is_absent(field, expected)
    actual_absent = _is_absent(field, actual)
    if expected_absent and not actual_absent:
        return "false_positive"
    if not expected_absent and actual_absent:
        return "unresolved"
    return "conflict"


def evaluate_projection(
    case: ReleaseRecognitionCase,
    actual: Mapping[str, object],
) -> list[FieldOutcome]:
    return [
        FieldOutcome(
            case_id=case.case_id,
            field=field,
            category=classify_field(field, case.expected[field], actual.get(field)),
            expected=case.expected[field],
            actual=actual.get(field),
        )
        for field in case.assert_fields
    ]


def recognition_metrics(outcomes: Iterable[FieldOutcome]) -> dict[str, dict[str, float | int]]:
    rows = list(outcomes)
    metrics: dict[str, dict[str, float | int]] = {}
    for field in (*FIELDS, "overall"):
        selected = rows if field == "overall" else [row for row in rows if row.field == field]
        counts = {category: 0 for category in CATEGORIES}
        for row in selected:
            counts[row.category] += 1
        total = len(selected)
        metrics[field] = {
            "total": total,
            **counts,
            "accuracy": (counts["matched"] / total) if total else 1.0,
        }
    return metrics


def format_recognition_report(outcomes: Iterable[FieldOutcome]) -> str:
    rows = list(outcomes)
    metrics = recognition_metrics(rows)
    lines = [
        "识别字段指标: "
        + ", ".join(
            f"{field}={metrics[field]['matched']}/{metrics[field]['total']}"
            for field in FIELDS
        )
    ]
    for row in rows:
        if row.category == "matched":
            continue
        lines.append(
            f"{row.case_id}.{row.field}: {row.category}; "
            f"expected={row.expected!r}, actual={row.actual!r}"
        )
    return "\n".join(lines)
