#!/usr/bin/env python3
"""用 Nyaa Torrent 文件清单评估 MediaFlux 自动整理识别能力。

安全边界：
- 只读取 Nyaa 搜索页与 ``.torrent`` 元信息，不下载媒体内容；
- 只运行真实 ``Organizer`` 的 dry-run，不写光鸭、不生成 STRM、不刷新媒体库；
- 落盘报告不保存 magnet、torrent URL、tracker、passkey 或 Token。

典型用法::

    python -m tools.benchmark_organize all --count 50
    python -m tools.benchmark_organize run --cases .superpowers/benchmarks/nyaa-organize/cases.jsonl

“自动率/人工确认率”可直接由干跑结果得到；“准确率”必须在 truth.jsonl 中补充真值后才会计算。
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import html
import json
import math
import re
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from bs4 import BeautifulSoup

from app import database as db
from app.clients.guangya import GuangYaFile
from app.config import get
from app.indexers.http import FixedHostHttpClient
from app.indexers.providers.base import require_html_response
from app.indexers.providers.nyaa import NyaaAdapter
from app.modules.download_dispatcher import BencodeError, TorrentManifest, parse_torrent_manifest
from app.modules.organize import OrganizePlan, OrganizeRules, Organizer
from app.modules.scraper import MatchResult, TMDBScraper

DEFAULT_OUTPUT_ROOT = Path(".superpowers/benchmarks/nyaa-organize")
DEFAULT_SEED = 20260811
DEFAULT_QUOTAS: dict[str, int] = {
    "release_noise": 10,
    "standard_episode": 8,
    "range_batch": 6,
    "specials": 10,
    "absolute_mapping": 9,
    "ambiguous": 7,
}
BUCKET_ORDER = (
    "specials",
    "absolute_mapping",
    "range_batch",
    "standard_episode",
    "release_noise",
    "ambiguous",
)
_VIDEO_EXTENSIONS = frozenset({
    "mkv", "mp4", "ts", "m2ts", "mts", "avi", "mov", "m4v", "webm",
    "mpeg", "mpg", "wmv", "flv", "vob", "tp", "f4v", "rm", "rmvb",
})
_SPECIAL_RE = re.compile(
    r"(?ix)(?:^|[\s._\-\[\]()])(?:OVA|OAD|ONA|SPECIALS?|SP\d*|NCOP|NCED|PV\d*|EXTRAS?|BONUS)(?:$|[\s._\-\[\]()])"
)
_RANGE_RE = re.compile(
    r"(?ix)(?:\[|\(|\b)(?:S\d{1,2})?[ ._-]?(?:E|EP)?\d{1,3}\s*[-~–—]\s*(?:E|EP)?\d{1,3}(?:\]|\)|\b)|"
    r"\b(?:BATCH|COMPLETE|COMPLETE[ ._-]?SERIES|SEASON[ ._-]?PACK|FIN(?:AL)?|END)\b|"
    r"(?:全集|全\s*\d+\s*集)"
)
_STANDARD_EPISODE_RES = (
    re.compile(r"(?i)\bS\d{1,2}E\d{1,4}\b"),
    re.compile(r"(?i)(?:^|[\s._\-\[])EP?\s*0?\d{1,3}(?:v\d+)?(?:$|[\s._\-\]])"),
    re.compile(r"(?:^|\s)-\s*0?\d{1,3}(?:v\d+)?(?:\s|$)"),
    re.compile(r"\[0?\d{1,3}(?:v\d+)?\]"),
)
_EPISODE_NUMBER_RES = (
    re.compile(r"(?i)\bS(?P<season>\d{1,2})E(?P<episode>\d{1,4})\b"),
    re.compile(r"(?i)(?:^|[\s._\-\[])EP?\s*0?(?P<episode>\d{1,3})(?:v\d+)?(?:$|[\s._\-\]])"),
    re.compile(r"(?:^|\s)-\s*0?(?P<episode>\d{1,3})(?:v\d+)?(?:\s|$)"),
)
_SECRET_PATTERNS = (
    re.compile(r"(?i)magnet:\?[^\s\"'<>]+"),
    re.compile(r"(?i)https?://[^\s\"'<>]+"),
    re.compile(r"(?i)authorization\s*:\s*bearer\s+[^\s,;\"']+"),
    re.compile(
        r"(?i)[\"']?(?:passkey|token|apikey|api_key|api-key|secret|password)"
        r"[\"']?\s*[:=]\s*[\"']?[^\s,;&}\"']+"
    ),
)


@dataclass(frozen=True)
class Candidate:
    title: str
    category: str
    size_text: str
    size_bytes: int | None
    seeders: int
    leechers: int
    downloads: int
    published_at: str
    torrent_url: str

    @property
    def key(self) -> str:
        return hashlib.sha256(
            f"{self.title}\x1f{self.torrent_url}".encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class CollectedCase:
    schema_version: int
    case_id: str
    bucket: str
    title: str
    category: str
    size_text: str
    size_bytes: int | None
    seeders: int
    leechers: int
    downloads: int
    published_at: str
    manifest_name: str
    manifest_version: str
    manifest_sha256: str
    files: tuple[dict[str, Any], ...]


class ManifestGuangYaClient:
    """把 Torrent 相对路径映射为只读 GuangYa 目录树。"""

    source_id = "__benchmark_source__"
    target_id = "__benchmark_target__"

    def __init__(self, case: dict[str, Any]):
        self._nodes: dict[str, GuangYaFile] = {}
        self._children: dict[str, list[GuangYaFile]] = {}
        self._add_node(GuangYaFile(
            file_id="0", name="根目录", is_dir=True, parent_id="",
        ))
        self._add_node(GuangYaFile(
            file_id=self.source_id,
            name=str(case.get("manifest_name") or case.get("title") or "Nyaa sample"),
            is_dir=True,
            parent_id="0",
        ))
        self._add_node(GuangYaFile(
            file_id=self.target_id,
            name="基准归档目标",
            is_dir=True,
            parent_id="0",
        ))
        self._build(case)

    @staticmethod
    def _node_id(case_id: str, kind: str, path: Sequence[str]) -> str:
        digest = hashlib.sha256(
            f"{case_id}\x1f{kind}\x1f{'/'.join(path)}".encode("utf-8")
        ).hexdigest()[:24]
        return f"benchmark-{kind}-{digest}"

    def _add_node(self, node: GuangYaFile) -> None:
        if node.file_id in self._nodes:
            raise ValueError(f"duplicate virtual node id: {node.file_id}")
        self._nodes[node.file_id] = node
        self._children.setdefault(node.file_id, [])
        if node.parent_id:
            self._children.setdefault(node.parent_id, []).append(node)

    def _build(self, case: dict[str, Any]) -> None:
        case_id = str(case.get("case_id") or "case")
        directory_ids: dict[tuple[str, ...], str] = {(): self.source_id}
        raw_files = case.get("files") or []
        if not isinstance(raw_files, list):
            raise ValueError("case files must be a list")
        for raw_file in raw_files:
            if not isinstance(raw_file, dict):
                raise ValueError("case file must be an object")
            raw_path = raw_file.get("path")
            if not isinstance(raw_path, list) or not raw_path:
                raise ValueError("case file path must be a non-empty list")
            parts = tuple(str(part) for part in raw_path)
            parent_path: tuple[str, ...] = ()
            for component in parts[:-1]:
                current_path = parent_path + (component,)
                if current_path not in directory_ids:
                    node_id = self._node_id(case_id, "dir", current_path)
                    self._add_node(GuangYaFile(
                        file_id=node_id,
                        name=component,
                        is_dir=True,
                        parent_id=directory_ids[parent_path],
                        etag=hashlib.sha1("/".join(current_path).encode()).hexdigest(),
                    ))
                    directory_ids[current_path] = node_id
                parent_path = current_path
            filename = parts[-1]
            try:
                length = max(0, int(raw_file.get("length") or 0))
            except (TypeError, ValueError, OverflowError):
                length = 0
            extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            file_id = self._node_id(case_id, "file", parts)
            self._add_node(GuangYaFile(
                file_id=file_id,
                name=filename,
                is_dir=False,
                size=length,
                etag=hashlib.sha1(f"{case_id}:{'/'.join(parts)}:{length}".encode()).hexdigest(),
                parent_id=directory_ids[parent_path],
                extension=extension,
            ))
        for children in self._children.values():
            children.sort(key=lambda item: (not item.is_dir, item.name.casefold(), item.file_id))

    def file_info(self, file_id: str) -> GuangYaFile | None:
        return self._nodes.get(str(file_id))

    def list_dir(self, dir_id: str) -> list[GuangYaFile]:
        node = self._nodes.get(str(dir_id))
        if node is None or not node.is_dir:
            raise FileNotFoundError(str(dir_id))
        return list(self._children.get(str(dir_id), ()))

    def create_dir(self, *_args, **_kwargs):  # pragma: no cover - dry-run guard
        raise AssertionError("benchmark client is read-only")

    def move(self, *_args, **_kwargs):  # pragma: no cover - dry-run guard
        raise AssertionError("benchmark client is read-only")

    def rename(self, *_args, **_kwargs):  # pragma: no cover - dry-run guard
        raise AssertionError("benchmark client is read-only")


def _normalize_title(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def bucket_tags(title: str) -> set[str]:
    """返回标题具备的边界标签；一个标题可同时属于多个桶。"""
    text = _normalize_title(title)
    upper = text.upper()
    tags: set[str] = set()
    if _SPECIAL_RE.search(text):
        tags.add("specials")
    if _RANGE_RE.search(text):
        tags.add("range_batch")
    episode_numbers: list[tuple[int | None, int]] = []
    for pattern in _EPISODE_NUMBER_RES:
        for match in pattern.finditer(text):
            season = match.groupdict().get("season")
            episode = match.groupdict().get("episode")
            if episode is not None:
                episode_numbers.append((int(season) if season else None, int(episode)))
    if any(episode > 12 or (season or 0) > 1 for season, episode in episode_numbers):
        tags.add("absolute_mapping")
    if any(pattern.search(text) for pattern in _STANDARD_EPISODE_RES):
        tags.add("standard_episode")
    bracket_groups = len(re.findall(r"\[[^\]]+\]", text))
    noisy_tokens = sum(
        token in upper
        for token in (
            "WEB-DL", "WEBRIP", "BLURAY", "BDRIP", "HEVC", "H265", "H.265",
            "10BIT", "1080P", "2160P", "MULTI", "FLAC", "AAC", "ASS", "CHS", "CHT",
        )
    )
    if bracket_groups >= 4 or noisy_tokens >= 4:
        tags.add("release_noise")
    if not tags:
        tags.add("ambiguous")
    return tags


def _stable_candidate_order(candidates: Iterable[Candidate], seed: int) -> list[Candidate]:
    return sorted(
        candidates,
        key=lambda item: hashlib.sha256(
            f"{seed}\x1f{item.key}".encode("utf-8")
        ).hexdigest(),
    )


def candidate_queues(
    candidates: Sequence[Candidate], *, seed: int,
) -> dict[str, list[Candidate]]:
    ordered = _stable_candidate_order(candidates, seed)
    return {
        bucket: [item for item in ordered if bucket in bucket_tags(item.title)]
        for bucket in BUCKET_ORDER
    }


def _candidate_from_item(item) -> Candidate | None:
    torrent_url = str(getattr(item, "torrent_url", "") or "").strip()
    if not torrent_url:
        return None
    published = getattr(item, "published_at", None)
    published_at = published.isoformat() if published is not None else ""
    return Candidate(
        title=str(getattr(item, "title", "") or "").strip(),
        category=str(getattr(item, "category", "") or "").strip(),
        size_text=str(getattr(item, "size_text", "") or "").strip(),
        size_bytes=getattr(item, "size_bytes", None),
        seeders=int(getattr(item, "seeders", 0) or 0),
        leechers=int(getattr(item, "leechers", 0) or 0),
        downloads=int(getattr(item, "downloads", 0) or 0),
        published_at=published_at,
        torrent_url=torrent_url,
    )


async def fetch_nyaa_candidates(
    http: FixedHostHttpClient, *, pages: int, query: str,
) -> list[Candidate]:
    adapter = NyaaAdapter(
        site_id="nyaa",
        site_name="Nyaa",
        base_url="https://nyaa.si/",
        http=http,
        default_enabled=True,
    )
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for page in range(1, max(1, pages) + 1):
        response = None
        for attempt in range(3):
            response = await http.get(
                "https://nyaa.si/",
                params={
                    "f": "0", "c": "1_0", "q": query,
                    "s": "id", "o": "desc", "p": str(page),
                },
            )
            if response.status_code not in {429, 500, 502, 503, 504}:
                break
            if attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
        assert response is not None
        adapter._validate_status(response.status_code)
        require_html_response(response)
        soup = BeautifulSoup(response.body, "lxml")
        table = soup.select_one("table.torrent-list")
        if table is None:
            if "no results found" in soup.get_text(" ", strip=True).lower():
                break
            raise RuntimeError("Nyaa search page structure is invalid")
        page_added = 0
        for row in soup.select("table.torrent-list tbody tr"):
            try:
                item = adapter._parse_row(row, adapter.base_url)
            except Exception:
                item = None
            candidate = _candidate_from_item(item) if item is not None else None
            if candidate is None or candidate.key in seen:
                continue
            seen.add(candidate.key)
            candidates.append(candidate)
            page_added += 1
        # Nyaa 的“下一页”通常只有 glyphicon，锚点没有可见文字。
        # 以本页是否带来新结果作为终止条件，可兼容图标分页，也能避免
        # 站点在越界页重复返回最后一页时无意义地继续请求。
        if page_added == 0:
            break
    return candidates


def _case_from_manifest(candidate: Candidate, bucket: str, body: bytes, manifest: TorrentManifest) -> CollectedCase:
    fingerprint = hashlib.sha256(body).hexdigest()
    case_id = f"nyaa-{fingerprint[:20]}"
    files = tuple(
        {"path": list(item.path), "length": int(item.length)}
        for item in manifest.files
    )
    return CollectedCase(
        schema_version=1,
        case_id=case_id,
        bucket=bucket,
        title=candidate.title,
        category=candidate.category,
        size_text=candidate.size_text,
        size_bytes=candidate.size_bytes,
        seeders=candidate.seeders,
        leechers=candidate.leechers,
        downloads=candidate.downloads,
        published_at=candidate.published_at,
        manifest_name=manifest.name,
        manifest_version=manifest.version,
        manifest_sha256=fingerprint,
        files=files,
    )


async def _fetch_manifest_case(
    http: FixedHostHttpClient, candidate: Candidate, bucket: str,
) -> tuple[CollectedCase | None, str]:
    try:
        response = await http.get(
            candidate.torrent_url,
            headers={"Accept": "application/x-bittorrent,application/octet-stream;q=0.9"},
        )
        if response.status_code != 200:
            return None, f"HTTP {response.status_code}"
        manifest = parse_torrent_manifest(response.body)
        if not any(
            item.path
            and item.path[-1].rsplit(".", 1)[-1].lower() in _VIDEO_EXTENSIONS
            for item in manifest.files
        ):
            return None, "Torrent 文件清单不含可整理视频"
        return _case_from_manifest(candidate, bucket, response.body, manifest), ""
    except (BencodeError, OSError, RuntimeError, ValueError) as exc:
        return None, _scrub_cell(f"{type(exc).__name__}: {str(exc)[:160]}")
    except Exception as exc:  # 外部站点异常只进入汇总，不泄露 URL
        return None, _scrub_cell(f"{type(exc).__name__}: {str(exc)[:160]}")


async def collect_cases(
    *, count: int, pages: int, seed: int, query: str,
    timeout_seconds: float, delay_seconds: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if count <= 0 or count > 500:
        raise ValueError("count must be between 1 and 500")
    quotas = scaled_quotas(count)
    http = FixedHostHttpClient(
        allowed_hosts={"nyaa.si"},
        timeout_seconds=timeout_seconds,
        max_response_bytes=10 * 1024 * 1024,
        max_redirects=3,
        user_agent="MediaFlux-Organize-Benchmark/1.0",
    )
    failures: Counter[str] = Counter()
    try:
        candidates = await fetch_nyaa_candidates(http, pages=pages, query=query)
        queues = candidate_queues(candidates, seed=seed)
        cases: list[CollectedCase] = []
        attempted: set[str] = set()
        selected_case_ids: set[str] = set()

        async def fill(bucket: str, wanted: int, queue: Sequence[Candidate]) -> None:
            cursor = 0
            while sum(1 for case in cases if case.bucket == bucket) < wanted and cursor < len(queue):
                batch: list[Candidate] = []
                while cursor < len(queue) and len(batch) < 3:
                    candidate = queue[cursor]
                    cursor += 1
                    if candidate.key in attempted:
                        continue
                    attempted.add(candidate.key)
                    batch.append(candidate)
                if not batch:
                    continue
                results = await asyncio.gather(*(
                    _fetch_manifest_case(http, candidate, bucket) for candidate in batch
                ))
                for case, error in results:
                    if case is None:
                        failures[error or "unknown error"] += 1
                        continue
                    if case.case_id in selected_case_ids:
                        continue
                    cases.append(case)
                    selected_case_ids.add(case.case_id)
                    if sum(1 for item in cases if item.bucket == bucket) >= wanted:
                        break
                if delay_seconds > 0:
                    await asyncio.sleep(delay_seconds)

        for bucket in BUCKET_ORDER:
            await fill(bucket, quotas.get(bucket, 0), queues.get(bucket, ()))

        if len(cases) < count:
            fallback = _stable_candidate_order(candidates, seed + 991)
            await fill("fallback", count - len(cases), fallback)
        cases = cases[:count]
        payloads = [asdict(case) for case in cases]
        summary = {
            "requested": count,
            "collected": len(payloads),
            "candidate_pool": len(candidates),
            "seed": seed,
            "pages": pages,
            "query": query,
            "requested_quotas": quotas,
            "observed_buckets": dict(Counter(case["bucket"] for case in payloads)),
            "failures": dict(failures.most_common(20)),
        }
        return payloads, summary
    finally:
        await http.aclose()


def scaled_quotas(count: int) -> dict[str, int]:
    if count == sum(DEFAULT_QUOTAS.values()):
        return dict(DEFAULT_QUOTAS)
    raw = {
        bucket: count * value / sum(DEFAULT_QUOTAS.values())
        for bucket, value in DEFAULT_QUOTAS.items()
    }
    quotas = {bucket: int(value) for bucket, value in raw.items()}
    remainder = count - sum(quotas.values())
    order = sorted(raw, key=lambda bucket: (-(raw[bucket] - quotas[bucket]), bucket))
    for bucket in order[:remainder]:
        quotas[bucket] += 1
    return quotas


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _sanitize_for_persistence(value: Any) -> Any:
    if isinstance(value, str):
        return _scrub_cell(value)
    if isinstance(value, dict):
        return {str(key): _sanitize_for_persistence(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_for_persistence(item) for item in value]
    return value


def write_json(path: Path, payload: Any) -> None:
    _atomic_write_text(
        path,
        json.dumps(
            _sanitize_for_persistence(payload),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n",
    )


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    lines = [
        json.dumps(_sanitize_for_persistence(row), ensure_ascii=False, sort_keys=True)
        for row in rows
    ]
    _atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: row must be an object")
        rows.append(value)
    return rows


def truth_template(cases: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "case_id": str(case.get("case_id") or ""),
            "expected": {
                "action": "",
                "provider": "tmdb",
                "media_type": "tv",
                "external_id": "",
                "season": None,
                "episodes": [],
                "plans": [],
                "plans_exact": False,
            },
            "notes": "",
        }
        for case in cases
    ]


def _safe_text(value: object, *, limit: int) -> str:
    return _scrub_cell(value)[:limit]


def _safe_finite_float(value: object, *, digits: int = 4) -> float:
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return round(number, digits)


def _safe_evidence_scalar(value: object, *, limit: int = 160) -> object:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return _safe_finite_float(value)
    if isinstance(value, str):
        return _safe_text(value, limit=limit)
    return None


def _safe_ai_evidence(match: MatchResult) -> dict[str, Any]:
    diagnostic = getattr(match, "ai_diagnostic", None)
    if not isinstance(diagnostic, dict) or not diagnostic:
        return {}
    output = diagnostic.get("output")
    revalidation = diagnostic.get("tmdb_revalidation")
    payload: dict[str, Any] = {
        "attempted": bool(diagnostic.get("attempted")),
        "reason": _safe_text(diagnostic.get("reason"), limit=120),
        "error": _safe_text(diagnostic.get("error"), limit=240),
    }
    if isinstance(output, dict):
        payload["output"] = {
            key: _safe_evidence_scalar(
                output.get(key), limit=500 if key == "title" else 64
            )
            for key in ("title", "year", "media_type", "confidence")
            if isinstance(output.get(key), (str, int, float, bool))
            or output.get(key) is None
        }
    if isinstance(revalidation, dict):
        source_anchor = revalidation.get("source_anchor")
        position = revalidation.get("position")
        corroboration = revalidation.get("tavily_corroboration")
        safe_revalidation: dict[str, Any] = {
            key: _safe_evidence_scalar(revalidation.get(key))
            for key in (
                "passed", "title_verified", "year_verified",
                "source_year_verified", "id_verified", "resolution_mode",
            )
            if isinstance(revalidation.get(key), (str, int, float, bool))
            or revalidation.get(key) is None
        }
        if isinstance(source_anchor, dict):
            safe_revalidation["source_anchor"] = {
                key: _safe_evidence_scalar(source_anchor.get(key))
                for key in ("passed", "score", "reason")
                if isinstance(source_anchor.get(key), (str, int, float, bool))
                or source_anchor.get(key) is None
            }
        if isinstance(position, dict):
            safe_revalidation["position"] = {
                key: _safe_evidence_scalar(position.get(key))
                for key in ("required", "passed", "reason", "season", "episode")
                if isinstance(position.get(key), (str, int, float, bool))
                or position.get(key) is None
            }
        if isinstance(corroboration, dict):
            safe_revalidation["tavily_corroboration"] = {
                key: _safe_evidence_scalar(corroboration.get(key))
                for key in (
                    "attempted", "status", "cached", "title_count", "passed",
                    "ai_tmdb_score", "source_web_score", "web_tmdb_score",
                    "strict_confidence", "strict_same_id",
                )
                if isinstance(corroboration.get(key), (str, int, float, bool))
                or corroboration.get(key) is None
            }
        payload["tmdb_revalidation"] = safe_revalidation
    return payload


def _safe_match(match: MatchResult | None) -> dict[str, Any]:
    if match is None:
        return {}
    context = getattr(match, "context", None)
    metadata = getattr(match, "metadata", None)
    evidence = metadata.get("recognition_evidence") if isinstance(metadata, dict) else None
    safe_evidence = {
        key: _safe_evidence_scalar(evidence.get(key))
        for key in (
            "mode", "tmdb_id", "llm_confidence", "source_web_score",
            "web_tmdb_score", "strict_tmdb_confidence",
        )
        if isinstance(evidence, dict)
        and (isinstance(evidence.get(key), (str, int, float, bool)) or evidence.get(key) is None)
    }
    return {
        "provider": _safe_text(
            getattr(match, "provider", "")
            or ("tmdb" if getattr(match, "tmdb_id", "") else ""),
            limit=64,
        ),
        "external_id": _safe_text(
            getattr(match, "external_id", "") or getattr(match, "tmdb_id", "") or "",
            limit=128,
        ),
        "tmdb_id": _safe_text(getattr(match, "tmdb_id", ""), limit=128),
        "title": _safe_text(getattr(match, "title", ""), limit=500),
        "year": _safe_text(getattr(match, "year", ""), limit=16),
        "media_type": _safe_text(getattr(match, "media_type", ""), limit=32),
        "confidence": _safe_finite_float(getattr(match, "confidence", 0.0)),
        "score": _safe_finite_float(getattr(match, "score", 0.0)),
        "need_confirm": bool(getattr(match, "need_confirm", False)),
        "status": _safe_text(getattr(match, "status", ""), limit=80),
        "matched_by": _safe_text(getattr(match, "matched_by", ""), limit=80),
        "error": _safe_text(getattr(match, "error", ""), limit=500),
        "normalized_title": _safe_text(
            getattr(context, "normalized_title", ""), limit=500
        ),
        "recognition_evidence": safe_evidence,
        "ai_evidence": _safe_ai_evidence(match),
    }


def _safe_mapping(plan: OrganizePlan) -> dict[str, Any] | None:
    mapping = getattr(plan, "episode_mapping", None)
    if mapping is None:
        return None
    payload: dict[str, Any] = {}
    for key in (
        "source_season", "source_episode", "target_season", "target_episode",
        "mode", "reason", "confidence",
    ):
        if hasattr(mapping, key):
            value = getattr(mapping, key)
            if isinstance(value, (str, int, float, bool)) or value is None:
                payload[key] = value
    return payload or None


def plan_payload(plan: OrganizePlan) -> dict[str, Any]:
    return {
        "action": str(plan.action or ""),
        "source_path": "/".join(part for part in (plan.original_path, plan.original_name) if part),
        "source_season": plan.source_season,
        "source_episode": plan.source_episode,
        "season": plan.season,
        "episode": plan.episode,
        "target_path": str(plan.target_path or ""),
        "new_name": str(plan.new_name or ""),
        "note": str(plan.note or "")[:500],
        "conflict_decision": str(plan.conflict_decision or ""),
        "conflict_note": str(plan.conflict_note or "")[:500],
        "match": _safe_match(plan.match),
        "episode_mapping": _safe_mapping(plan),
    }


def observed_action(plans: Sequence[dict[str, Any]], stats: dict[str, Any]) -> str:
    video_plans = [
        plan for plan in plans
        if str(plan.get("source_path") or "").rsplit(".", 1)[-1].lower() in _VIDEO_EXTENSIONS
    ]
    if not video_plans:
        return "skip"
    if int(stats.get("failed", 0) or 0) > 0 or any(plan.get("action") == "conflict" for plan in video_plans):
        return "error"
    if int(stats.get("need_confirm", 0) or 0) > 0 or any(
        bool((plan.get("match") or {}).get("need_confirm")) for plan in video_plans
    ):
        return "confirm"
    if all(plan.get("action") == "move" and (plan.get("match") or {}).get("external_id") for plan in video_plans):
        return "auto"
    return "skip"


def observed_outcome(plans: Sequence[dict[str, Any]], stats: dict[str, Any], action: str) -> str:
    """把粗粒度 action 投影成可用于回归门禁的结果类别。

    ``observed_action`` 保持兼容；``observed_outcome`` 额外区分安全隔离、
    未识别和运行错误，避免把所有 skip 混成一个无法定位的数字。
    自动结果是否正确仍需结合人工真值在 :func:`score_results` 中判定。
    """
    if action == "auto":
        return "automatic_unverified"
    if action == "confirm":
        return "needs_confirmation"
    if action == "error":
        return "runtime_error"
    reasons = [str(item or "") for item in (stats.get("skip_reasons") or [])]
    reasons.extend(str(plan.get("note") or "") for plan in plans)
    joined = "；".join(reasons)
    if any(marker in joined for marker in ("隔离", "保留在源目录", "等待人工识别")):
        return "isolated"
    return "unrecognized"


def run_benchmark_case(
    case: dict[str, Any], *, scraper: TMDBScraper, base_rules: OrganizeRules,
) -> dict[str, Any]:
    client = ManifestGuangYaClient(case)
    organizer = Organizer(client=client, scraper=scraper)
    started = time.monotonic()
    try:
        plans, stats = organizer.organize(
            client.source_id,
            base_rules,
            dry_run=True,
            post_actions=False,
            source_name=str(case.get("manifest_name") or case.get("title") or ""),
            require_complete_scan=True,
            media_probe_cache_only=True,
            automatic=True,
        )
        plan_rows = [plan_payload(plan) for plan in plans]
        action = observed_action(plan_rows, stats)
        outcome = observed_outcome(plan_rows, stats, action)
        error = ""
    except Exception as exc:
        plan_rows = []
        stats = {}
        action = "error"
        outcome = "runtime_error"
        error = f"{type(exc).__name__}: {str(exc)[:500]}"
    return {
        "case_id": str(case.get("case_id") or ""),
        "bucket": str(case.get("bucket") or ""),
        "title": str(case.get("title") or ""),
        "manifest_name": str(case.get("manifest_name") or ""),
        "manifest_version": str(case.get("manifest_version") or ""),
        "manifest_files": len(case.get("files") or []),
        "manifest_video_files": sum(
            1 for item in (case.get("files") or [])
            if isinstance(item, dict)
            and isinstance(item.get("path"), list)
            and item["path"]
            and str(item["path"][-1]).rsplit(".", 1)[-1].lower() in _VIDEO_EXTENSIONS
        ),
        "observed_action": action,
        "observed_outcome": outcome,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "error": error,
        "stats": {
            key: stats.get(key)
            for key in (
                "total", "matched", "need_confirm", "skipped", "failed",
                "scan_complete", "scan_errors", "skip_reasons", "confirmations",
                "scan_elapsed_seconds", "recognition_elapsed_seconds",
                "conflict_check_elapsed_seconds", "total_elapsed_seconds",
                "tmdb_search_requests", "tmdb_search_cache_hits",
                "tmdb_detail_requests", "tmdb_detail_cache_hits",
                "tmdb_season_detail_requests", "tmdb_season_detail_cache_hits",
                "ai_requests", "ai_cache_hits",
                "tavily_hint_lookups", "tavily_hint_requests",
                "tavily_hint_cache_hits", "tavily_hint_matches",
                "directory_identity_cache_hits", "directory_identity_cache_groups",
                "recognition_work_cache_hits", "recognition_work_cache_groups",
                "directory_special_identity_bindings",
            )
        },
        "plans": plan_rows,
    }


def _truth_index(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        case_id = str(row.get("case_id") or "").strip()
        expected = row.get("expected")
        if case_id and isinstance(expected, dict) and str(expected.get("action") or "").strip():
            result[case_id] = expected
    return result


def _normalized_expected_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _expected_plan_matches(plan: dict[str, Any], expected_plan: dict[str, Any]) -> bool:
    for key in ("season", "episode", "source_season", "source_episode"):
        if key not in expected_plan:
            continue
        expected_value = _normalized_expected_int(expected_plan.get(key))
        if expected_value is None or plan.get(key) != expected_value:
            return False
    match = plan.get("match") or {}
    for key in ("provider", "external_id", "media_type"):
        expected_value = str(expected_plan.get(key) or "").strip()
        if expected_value and str(match.get(key) or "") != expected_value:
            return False
    return True


def _auto_match_checks(
    result: dict[str, Any], expected: dict[str, Any],
) -> tuple[bool, bool]:
    plans = [plan for plan in result.get("plans") or [] if plan.get("action") == "move"]
    if not plans:
        return False, False

    expected_provider = str(expected.get("provider") or "").strip()
    expected_id = str(expected.get("external_id") or "").strip()
    expected_type = str(expected.get("media_type") or "").strip()
    for plan in plans:
        match = plan.get("match") or {}
        if expected_provider and str(match.get("provider") or "") != expected_provider:
            return False, False
        if expected_id and str(match.get("external_id") or "") != expected_id:
            return False, False
        if expected_type and str(match.get("media_type") or "") != expected_type:
            return False, False

    expected_season = expected.get("season")
    if expected_season is not None:
        normalized_season = _normalized_expected_int(expected_season)
        if normalized_season is None or any(plan.get("season") != normalized_season for plan in plans):
            return True, False

    expected_episodes = {
        int(item) for item in (expected.get("episodes") or [])
        if isinstance(item, int) or (isinstance(item, str) and item.isdigit())
    }
    if expected_episodes:
        observed = {int(plan["episode"]) for plan in plans if isinstance(plan.get("episode"), int)}
        if observed != expected_episodes:
            return True, False

    expected_plans = expected.get("plans") or []
    if not isinstance(expected_plans, list):
        return True, False
    if expected_plans:
        observed_by_source: dict[str, dict[str, Any]] = {}
        for plan in plans:
            source_path = str(plan.get("source_path") or "")
            if not source_path or source_path in observed_by_source:
                return True, False
            observed_by_source[source_path] = plan
        expected_sources: set[str] = set()
        for expected_plan in expected_plans:
            if not isinstance(expected_plan, dict):
                return True, False
            source_path = str(expected_plan.get("source_path") or "")
            if not source_path or source_path in expected_sources:
                return True, False
            expected_sources.add(source_path)
            observed_plan = observed_by_source.get(source_path)
            if observed_plan is None or not _expected_plan_matches(observed_plan, expected_plan):
                return True, False
        if bool(expected.get("plans_exact")) and expected_sources != set(observed_by_source):
            return True, False
    return True, True


def score_results(results: Sequence[dict[str, Any]], truths: Sequence[dict[str, Any]]) -> dict[str, Any]:
    truth_by_id = _truth_index(truths)
    counts: Counter[str] = Counter()
    labeled_rows: list[dict[str, Any]] = []
    for result in results:
        expected = truth_by_id.get(str(result.get("case_id") or ""))
        if expected is None:
            continue
        observed = str(result.get("observed_action") or "")
        expected_action = str(expected.get("action") or "")
        move_plans = [
            plan for plan in (result.get("plans") or [])
            if plan.get("action") == "move"
        ]
        if move_plans:
            identity_ok, mapping_ok = _auto_match_checks(result, expected)
        elif observed == "auto":
            # 批次被归类为自动整理却没有任何移动计划，不能计为正确自动命中。
            identity_ok, mapping_ok = False, False
        else:
            identity_ok, mapping_ok = True, True
        unsafe_move = bool(move_plans) and (
            expected_action != "auto" or not (identity_ok and mapping_ok)
        )
        auto_result_ok = identity_ok and mapping_ok and not unsafe_move
        correct = (
            observed == expected_action
            and not unsafe_move
            and (expected_action != "auto" or auto_result_ok)
        )
        counts["labeled"] += 1
        counts["correct"] += int(correct)
        counts["observed_auto"] += int(observed == "auto")
        counts["expected_confirmation"] += int(expected_action == "confirm")
        if unsafe_move and observed != "auto":
            counts["unsafe_move_in_non_auto_batch"] += 1
        if move_plans and identity_ok and not mapping_ok:
            counts["season_episode_errors"] += 1
        expected_provider = str(expected.get("provider") or "").strip().lower()
        expected_external_id = str(expected.get("external_id") or "").strip()
        if expected_provider == "tmdb" and expected_external_id and move_plans:
            if any(
                str((plan.get("match") or {}).get("external_id") or "").strip()
                != expected_external_id
                for plan in move_plans
            ):
                counts["tmdb_id_errors"] += 1
        if observed == "auto" and expected_action == "auto" and auto_result_ok:
            outcome = "automatic_correct"
            counts["automatic_correct"] += 1
        elif observed == "auto":
            outcome = "wrong_match"
            counts["wrong_match"] += 1
            counts["dangerous_false_auto"] += 1
        elif observed == "confirm":
            outcome = "needs_confirmation"
            counts["needs_confirmation"] += 1
        elif observed == "error":
            outcome = "runtime_error"
            counts["runtime_error"] += 1
        else:
            raw_outcome = str(result.get("observed_outcome") or "unrecognized")
            outcome = raw_outcome if raw_outcome in {"isolated", "unrecognized"} else "unrecognized"
            counts[outcome] += 1
        if expected_action == "auto" and observed == "confirm":
            counts["unnecessary_confirmation"] += 1
        if expected_action == "confirm" and observed == "confirm":
            counts["expected_confirmation_intercepted"] += 1
        labeled_rows.append({
            "case_id": result.get("case_id"),
            "expected_action": expected_action,
            "observed_action": observed,
            "outcome": outcome,
            "identity_correct": identity_ok,
            "mapping_correct": mapping_ok,
            "unsafe_move": unsafe_move,
            "correct": correct,
        })
    labeled = counts["labeled"]
    observed_auto = counts["observed_auto"]
    expected_confirmation = counts["expected_confirmation"]
    return {
        **dict(counts),
        "truth_coverage": round(labeled / len(results), 4) if results else 0.0,
        "accuracy": round(counts["correct"] / labeled, 4) if labeled else None,
        "auto_precision": (
            round(counts["automatic_correct"] / observed_auto, 4)
            if observed_auto else None
        ),
        "confirmation_recall": (
            round(counts["expected_confirmation_intercepted"] / expected_confirmation, 4)
            if expected_confirmation else None
        ),
        "rows": labeled_rows,
        "note": (
            "准确率只统计 truth.jsonl 中已填写 action 的样本；任何批次只要包含错误移动计划，整条样本即判错。"
            if labeled else "尚无人工真值，未计算准确率。"
        ),
    }


def summarize_results(results: Sequence[dict[str, Any]], scoring: dict[str, Any]) -> dict[str, Any]:
    actions = Counter(str(row.get("observed_action") or "unknown") for row in results)
    outcomes = Counter(str(row.get("observed_outcome") or "unknown") for row in results)
    total = len(results)
    files = sum(int(row.get("manifest_video_files") or 0) for row in results)
    return {
        "cases": total,
        "video_files": files,
        "actions": dict(actions),
        "outcomes": dict(outcomes),
        "automation_rate": round(actions["auto"] / total, 4) if total else 0.0,
        "manual_confirmation_rate": round(actions["confirm"] / total, 4) if total else 0.0,
        "skip_rate": round(actions["skip"] / total, 4) if total else 0.0,
        "error_rate": round(actions["error"] / total, 4) if total else 0.0,
        "bucket_actions": {
            bucket: dict(Counter(
                str(row.get("observed_action") or "unknown")
                for row in results if row.get("bucket") == bucket
            ))
            for bucket in sorted({str(row.get("bucket") or "") for row in results})
        },
        "truth_scoring": scoring,
    }


def _scrub_cell(value: object) -> str:
    text = str(value if value is not None else "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def write_csv_report(path: Path, results: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=(
            "case_id", "bucket", "observed_action", "observed_outcome", "manifest_video_files",
            "elapsed_seconds", "title", "matched", "need_confirm", "skipped",
            "failed", "tmdb_search_requests", "ai_requests",
            "tavily_hint_requests", "tavily_hint_cache_hits",
            "tavily_hint_matches", "error",
        ))
        writer.writeheader()
        for result in results:
            stats = result.get("stats") or {}
            writer.writerow({
                "case_id": _scrub_cell(result.get("case_id")),
                "bucket": _scrub_cell(result.get("bucket")),
                "observed_action": _scrub_cell(result.get("observed_action")),
                "observed_outcome": _scrub_cell(result.get("observed_outcome")),
                "manifest_video_files": result.get("manifest_video_files", 0),
                "elapsed_seconds": result.get("elapsed_seconds", 0),
                "title": _scrub_cell(result.get("title")),
                "matched": stats.get("matched", 0),
                "need_confirm": stats.get("need_confirm", 0),
                "skipped": stats.get("skipped", 0),
                "failed": stats.get("failed", 0),
                "tmdb_search_requests": stats.get("tmdb_search_requests", 0),
                "ai_requests": stats.get("ai_requests", 0),
                "tavily_hint_requests": stats.get("tavily_hint_requests", 0),
                "tavily_hint_cache_hits": stats.get("tavily_hint_cache_hits", 0),
                "tavily_hint_matches": stats.get("tavily_hint_matches", 0),
                "error": _scrub_cell(result.get("error")),
            })
    temporary.replace(path)


def write_html_report(path: Path, summary: dict[str, Any], results: Sequence[dict[str, Any]]) -> None:
    action_labels = {"auto": "自动通过", "confirm": "人工确认", "skip": "跳过", "error": "错误"}
    rows = []
    for result in results:
        stats = result.get("stats") or {}
        rows.append(
            "<tr>"
            f"<td><code>{html.escape(_scrub_cell(result.get('case_id')))}</code></td>"
            f"<td>{html.escape(_scrub_cell(result.get('bucket')))}</td>"
            f"<td class=\"action-{html.escape(_scrub_cell(result.get('observed_action')))}\">"
            f"{html.escape(action_labels.get(str(result.get('observed_action')), str(result.get('observed_action'))))}</td>"
            f"<td>{int(result.get('manifest_video_files') or 0)}</td>"
            f"<td>{html.escape(_scrub_cell(result.get('title')))}</td>"
            f"<td>{int(stats.get('matched') or 0)} / {int(stats.get('need_confirm') or 0)} / {int(stats.get('skipped') or 0)}</td>"
            f"<td>{float(result.get('elapsed_seconds') or 0):.3f}s</td>"
            f"<td>{html.escape(_scrub_cell(result.get('error')))}</td>"
            "</tr>"
        )
    scoring = summary.get("truth_scoring") or {}
    accuracy = scoring.get("accuracy")
    accuracy_text = "未标注" if accuracy is None else f"{float(accuracy) * 100:.1f}%"
    truth_coverage_text = f"{float(scoring.get('truth_coverage') or 0) * 100:.1f}%"
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MediaFlux Nyaa 自动整理基准</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;margin:28px;color:#0f172a;background:#f8fafc}}
main{{max-width:1500px;margin:auto}} .metrics{{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px;margin:18px 0}}
.metric{{background:white;border:1px solid #e2e8f0;border-radius:10px;padding:14px}} .metric b{{display:block;font-size:24px;margin-top:6px}}
table{{width:100%;border-collapse:collapse;background:white;border:1px solid #e2e8f0}} th,td{{padding:9px 10px;border-bottom:1px solid #e2e8f0;text-align:left;vertical-align:top}} th{{position:sticky;top:0;background:#f1f5f9}}
.action-auto{{color:#047857}} .action-confirm{{color:#b45309}} .action-error{{color:#b91c1c}} code{{font-size:12px}} .note{{color:#64748b}}
@media(max-width:900px){{.metrics{{grid-template-columns:1fr 1fr}} table{{font-size:12px}}}}
</style></head><body><main>
<h1>MediaFlux Nyaa 自动整理基准</h1><p class="note">只读取 Torrent 文件清单并执行真实 Organizer dry-run；没有下载媒体，也没有写入云盘。识别回归固定使用 automatic=true、media_probe_enabled=false、media_probe_cache_only=true，不代表完整写入执行准确率。</p>
<div class="metrics">
<div class="metric">样本<b>{int(summary.get('cases') or 0)}</b></div>
<div class="metric">自动率<b>{float(summary.get('automation_rate') or 0)*100:.1f}%</b></div>
<div class="metric">人工确认率<b>{float(summary.get('manual_confirmation_rate') or 0)*100:.1f}%</b></div>
<div class="metric">错误率<b>{float(summary.get('error_rate') or 0)*100:.1f}%</b></div>
<div class="metric">真值覆盖率<b>{html.escape(truth_coverage_text)}</b></div>
<div class="metric">真值准确率<b>{html.escape(accuracy_text)}</b></div>
</div>
<p class="note">{html.escape(str(scoring.get('note') or ''))}</p>
<table><thead><tr><th>Case</th><th>边界桶</th><th>结果</th><th>视频</th><th>Nyaa 标题</th><th>匹配/确认/跳过</th><th>耗时</th><th>错误</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></main></body></html>"""
    _atomic_write_text(path, document)


@contextmanager
def isolated_benchmark_database():
    """为基准创建一次性 SQLite，避免测试学习结果污染运行数据库。"""
    original_path = Path(db.DB_PATH)
    original_test_mode = bool(getattr(db, "_configured_test_mode", False))
    with tempfile.TemporaryDirectory(prefix="mediaflux-organize-benchmark-") as directory:
        db.configure_database(Path(directory) / "benchmark.db", test_mode=True)
        try:
            from app.modules.recognition_knowledge import invalidate_active_cache

            invalidate_active_cache()
            db.init_db()
            yield
        finally:
            db.configure_database(original_path, test_mode=original_test_mode)
            try:
                from app.modules.recognition_knowledge import invalidate_active_cache

                invalidate_active_cache()
            except Exception:
                pass


def run_cases(
    cases: Sequence[dict[str, Any]], *, truth_rows: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with isolated_benchmark_database():
        return _run_cases_isolated(cases, truth_rows=truth_rows)


def _run_cases_isolated(
    cases: Sequence[dict[str, Any]], *, truth_rows: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    scraper = TMDBScraper()
    rules = replace(
        OrganizeRules.from_config(target_dir_id=ManifestGuangYaClient.target_id),
        target_dir_id=ManifestGuangYaClient.target_id,
        small_file_mb=0,
        clean_empty=False,
        link_strm=False,
        media_info_enabled=False,
        media_probe_enabled=False,
        notify_enabled=False,
        library_notify=False,
        strm_detail_notify=False,
        emby_refresh=False,
    )
    results: list[dict[str, Any]] = []
    for index, case in enumerate(cases, start=1):
        result = run_benchmark_case(case, scraper=scraper, base_rules=rules)
        results.append(result)
        print(
            f"[{index:02d}/{len(cases):02d}] {result['observed_action']:<7} "
            f"{result['elapsed_seconds']:>7.3f}s  {str(result['title'])[:90]}",
            flush=True,
        )
    scoring = score_results(results, truth_rows)
    summary = summarize_results(results, scoring)
    summary["database_mode"] = "isolated"
    return results, summary


def _resolve_output_dir(value: str) -> Path:
    if value:
        return Path(value).expanduser()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return DEFAULT_OUTPUT_ROOT / stamp


def _write_collection(output_dir: Path, cases: Sequence[dict[str, Any]], collection: dict[str, Any]) -> None:
    write_jsonl(output_dir / "cases.jsonl", cases)
    write_json(output_dir / "collection.json", collection)
    truth_path = output_dir / "truth.jsonl"
    if not truth_path.exists():
        write_jsonl(truth_path, truth_template(cases))


def _write_benchmark(output_dir: Path, results: Sequence[dict[str, Any]], summary: dict[str, Any]) -> None:
    write_json(output_dir / "report.json", {"summary": summary, "results": list(results)})
    write_csv_report(output_dir / "report.csv", results)
    write_html_report(output_dir / "report.html", summary, results)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.benchmark_organize",
        description="Nyaa Torrent 文件清单 + 真实 Organizer dry-run 自动整理基准",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_collection_args(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument("--count", type=int, default=50, help="目标样本数，默认 50")
        command_parser.add_argument("--pages", type=int, default=10, help="最多读取的 Nyaa 页面数")
        command_parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="确定性抽样种子")
        command_parser.add_argument("--query", default="", help="可选 Nyaa 搜索词")
        command_parser.add_argument("--timeout", type=float, default=15.0, help="单次 HTTP 超时秒数")
        command_parser.add_argument("--delay", type=float, default=0.15, help="每批 Torrent 元信息请求间隔")
        command_parser.add_argument("--output-dir", default="", help="报告目录，默认写入 .superpowers")

    collect_parser = subparsers.add_parser("collect", help="采集 Nyaa 标题与 Torrent 文件清单")
    add_collection_args(collect_parser)

    run_parser = subparsers.add_parser("run", help="对已采集 cases.jsonl 执行真实 Organizer dry-run")
    run_parser.add_argument("--cases", required=True, help="cases.jsonl 路径")
    run_parser.add_argument("--truth", default="", help="可选 truth.jsonl 路径")
    run_parser.add_argument("--output-dir", default="", help="报告目录；默认使用 cases 所在目录")

    all_parser = subparsers.add_parser("all", help="采集并立即运行完整基准")
    add_collection_args(all_parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {"collect", "all"}:
        output_dir = _resolve_output_dir(args.output_dir)
        print("安全模式：只读取 Nyaa 搜索页和 .torrent 元信息，不下载视频。", flush=True)
        cases, collection = asyncio.run(collect_cases(
            count=args.count,
            pages=max(1, args.pages),
            seed=args.seed,
            query=str(args.query or "").strip(),
            timeout_seconds=max(2.0, float(args.timeout)),
            delay_seconds=max(0.0, float(args.delay)),
        ))
        _write_collection(output_dir, cases, collection)
        print(f"已采集 {len(cases)}/{args.count} 个样本：{output_dir / 'cases.jsonl'}", flush=True)
        if args.command == "collect":
            return 0 if len(cases) == args.count else 2
        if not cases:
            return 2
        truth_rows = read_jsonl(output_dir / "truth.jsonl")
    else:
        cases_path = Path(args.cases).expanduser()
        cases = read_jsonl(cases_path)
        output_dir = Path(args.output_dir).expanduser() if args.output_dir else cases_path.parent
        truth_path = Path(args.truth).expanduser() if args.truth else output_dir / "truth.jsonl"
        truth_rows = read_jsonl(truth_path) if truth_path.exists() else []
        if not truth_path.exists():
            write_jsonl(truth_path, truth_template(cases))

    if not str(get("TMDB_API_KEY", "") or "").strip():
        print("警告：未配置 TMDB_API_KEY；本轮只能验证降级/人工确认路径，不能代表识别能力。", file=sys.stderr)
    results, summary = run_cases(cases, truth_rows=truth_rows)
    _write_benchmark(output_dir, results, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
    print(f"HTML 报告：{output_dir / 'report.html'}", flush=True)
    return 0 if summary.get("error_rate", 0.0) == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
