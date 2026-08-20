"""光鸭目录级 TMDB 搜索、预览与短期状态存储。"""
from __future__ import annotations

import dataclasses
import threading
import uuid
from dataclasses import asdict, dataclass
from time import monotonic
from typing import Callable

from app import config, database as db
from app.clients.guangya import GuangYaClient, GuangYaFile
from app.logger import get_logger
from app.modules.directory_media import DirectoryInspection, DirectoryMediaInspector
from app.modules.directory_scrape_errors import (
    DirectoryScrapeConflictError,
    DirectoryScrapeGoneError,
    DirectoryScrapeRequestError,
)
from app.modules.organize import (
    OrganizePlan,
    OrganizeRules,
    Organizer,
    enforce_fixed_organize_rules,
)
from app.modules.organize_sources import normalize_organize_sources
from app.modules.episode_mapping import (
    EpisodeMappingPlan,
    NUMBERING_MODES,
    build_directory_episode_evidence,
    infer_episode_mapping,
    infer_merged_season_cour_mapping,
    normalize_numbering_mode,
    season_episode_counts,
)
from app.modules.organize_delete_audit import DeleteCandidate, execute_recycle_bin_delete
from app.modules.organize_postprocess import companion_target_name, media_role
from app.modules.scraper import Candidate, MatchResult, ReleaseParseResult, TMDBScraper
from app.modules.subtitle_identity import plan_subtitle_companions
from app.modules.special_media import is_special_media_name, is_special_path


logger = get_logger(__name__)


def _tmdb_image_url(path: object, size: str) -> str:
    value = str(path or "").strip()
    if not value:
        return ""
    if value.startswith("https://"):
        return value
    if value.startswith("http://"):
        return ""
    return f"https://image.tmdb.org/t/p/{size}/{value.lstrip('/')}"


def _unique_names(items: object, key: str = "name") -> list[str]:
    if not isinstance(items, list):
        return []
    result: list[str] = []
    for item in items:
        value = item.get(key) if isinstance(item, dict) else item
        name = str(value or "").strip()
        if name and name not in result:
            result.append(name)
    return result


@dataclass
class InspectionRecord:
    owner: str
    inspection: DirectoryInspection
    rules: OrganizeRules
    scope_type: str
    scope_id: str
    created_at: float


@dataclass
class PreviewRecord:
    owner: str
    inspection_id: str
    inspection: DirectoryInspection
    rules: OrganizeRules
    scope_type: str
    scope_id: str
    match: MatchResult
    detail: dict
    plans: list[OrganizePlan]
    companion_plans: list[dict]
    signature: tuple
    target_snapshot: tuple
    stats: dict
    payload: dict
    season_override: int | None
    episode_override: int | None
    numbering_mode: str
    position_overrides: dict[object, tuple[int | None, int | None]]
    created_at: float
    claimed: bool = False


class DirectoryScrapeStore:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = monotonic,
        ttl_seconds: float = 600.0,
        max_records: int = 64,
    ) -> None:
        self._clock = clock
        self._ttl = max(1.0, float(ttl_seconds))
        self._max_records = max(1, int(max_records))
        self._lock = threading.RLock()
        self._inspections: dict[str, InspectionRecord] = {}
        self._previews: dict[str, PreviewRecord] = {}

    @staticmethod
    def _rules_copy(rules: OrganizeRules) -> OrganizeRules:
        return enforce_fixed_organize_rules(OrganizeRules(**asdict(rules)))

    def _prune(self) -> None:
        now = self._clock()
        self._inspections = {
            key: value
            for key, value in self._inspections.items()
            if now - value.created_at <= self._ttl
        }
        self._previews = {
            key: value
            for key, value in self._previews.items()
            if now - value.created_at <= self._ttl
        }
        for records in (self._inspections, self._previews):
            overflow = len(records) - self._max_records
            if overflow > 0:
                oldest = sorted(records, key=lambda key: records[key].created_at)[:overflow]
                for key in oldest:
                    records.pop(key, None)

    def put_inspection(
        self,
        owner: str,
        inspection: DirectoryInspection,
        rules: OrganizeRules,
        *,
        scope_type: str = "directory",
        scope_id: str = "",
    ) -> str:
        with self._lock:
            self._prune()
            record_id = uuid.uuid4().hex
            self._inspections[record_id] = InspectionRecord(
                owner=str(owner),
                inspection=inspection,
                rules=self._rules_copy(rules),
                scope_type=str(scope_type),
                scope_id=str(scope_id or inspection.directory_id),
                created_at=self._clock(),
            )
            self._prune()
            return record_id

    def get_inspection(self, owner: str, inspection_id: str) -> InspectionRecord:
        with self._lock:
            self._prune()
            record = self._inspections.get(str(inspection_id))
            if record is None or record.owner != str(owner):
                raise DirectoryScrapeGoneError("检查记录不存在或已过期")
            return record

    def put_preview(
        self,
        owner: str,
        inspection_id: str,
        inspection: DirectoryInspection,
        rules: OrganizeRules,
        match: MatchResult,
        detail: dict,
        plans: list[OrganizePlan],
        companion_plans: list[dict],
        signature: tuple,
        target_snapshot: tuple,
        stats: dict,
        payload: dict,
        *,
        scope_type: str = "directory",
        scope_id: str = "",
        season_override: int | None = None,
        episode_override: int | None = None,
        numbering_mode: str = "auto",
        position_overrides: dict[object, tuple[int | None, int | None]] | None = None,
    ) -> str:
        with self._lock:
            self._prune()
            preview_id = uuid.uuid4().hex
            self._previews[preview_id] = PreviewRecord(
                owner=str(owner),
                inspection_id=str(inspection_id),
                inspection=inspection,
                rules=self._rules_copy(rules),
                scope_type=str(scope_type),
                scope_id=str(scope_id or inspection.directory_id),
                match=dataclasses.replace(match),
                detail=dict(detail),
                plans=list(plans),
                companion_plans=[dict(item) for item in companion_plans],
                signature=signature,
                target_snapshot=target_snapshot,
                stats=dict(stats),
                payload=dict(payload),
                season_override=season_override,
                episode_override=episode_override,
                numbering_mode=normalize_numbering_mode(numbering_mode),
                position_overrides=dict(position_overrides or {}),
                created_at=self._clock(),
            )
            self._prune()
            return preview_id

    def get_preview(self, owner: str, preview_id: str) -> PreviewRecord:
        with self._lock:
            self._prune()
            record = self._previews.get(str(preview_id))
            if record is None or record.owner != str(owner):
                raise DirectoryScrapeGoneError("预览记录不存在或已过期")
            return record

    def claim_preview(self, owner: str, preview_id: str) -> PreviewRecord:
        with self._lock:
            record = self.get_preview(owner, preview_id)
            if record.claimed:
                raise DirectoryScrapeConflictError("该预览正在执行")
            record.claimed = True
            return record

    def release_preview(self, owner: str, preview_id: str) -> None:
        with self._lock:
            record = self._previews.get(str(preview_id))
            if record is not None and record.owner == str(owner):
                record.claimed = False

    def consume_preview(self, owner: str, preview_id: str) -> None:
        with self._lock:
            record = self._previews.get(str(preview_id))
            if record is not None and record.owner == str(owner):
                self._previews.pop(str(preview_id), None)


class FixedMatchScraper:
    supports_parent_path = True
    # 手动刮削已经由用户确认媒体身份与季号；允许仅提供季号生成预览。
    manual_position_confirmed = True

    def __init__(
        self,
        delegate: TMDBScraper,
        match: MatchResult,
        detail: dict | None = None,
        *,
        season_override: int | None = None,
        episode_override: int | None = None,
        preserve_specials: bool = False,
        position_overrides: dict[object, tuple[int | None, int | None]] | None = None,
    ) -> None:
        self.delegate = delegate
        self.fixed_match = dataclasses.replace(match)
        self.detail = dict(detail or {})
        self.season_override = season_override
        self.episode_override = episode_override
        self.preserve_specials = bool(preserve_specials)
        self.position_overrides = dict(position_overrides or {})

    def match(self, _filename: str, _parent_path: str = "") -> MatchResult:
        return dataclasses.replace(self.fixed_match)

    def _position_override(
        self, filename: str, parent_path: str = ""
    ) -> tuple[int | None, int | None] | None:
        normalized_parent = str(parent_path or "").replace("\\", "/").strip("/")
        normalized_filename = str(filename)
        position = self.position_overrides.get((str(parent_path or ""), normalized_filename))
        if position is None and normalized_parent:
            suffix_matches: list[tuple[int | None, int | None]] = []
            for key, candidate in self.position_overrides.items():
                if not isinstance(key, tuple) or len(key) != 2:
                    continue
                relative_parent, override_filename = key
                if str(override_filename) != normalized_filename:
                    continue
                normalized_relative = str(relative_parent or "").replace("\\", "/").strip("/")
                if normalized_relative and (
                    normalized_parent == normalized_relative
                    or normalized_parent.endswith(f"/{normalized_relative}")
                ):
                    suffix_matches.append(candidate)
            if len(suffix_matches) == 1:
                position = suffix_matches[0]
        if position is None:
            position = self.position_overrides.get(normalized_filename)
        return position

    def _apply_manual_position(
        self, season: int | None, episode: int | None
    ) -> tuple[int | None, int | None]:
        if self.fixed_match.media_type != "tv":
            return season, episode
        if self.season_override is not None and not (
            self.preserve_specials and season == 0
        ):
            season = self.season_override
        if self.episode_override is not None:
            episode = self.episode_override
        return season, episode

    def parse_media(
        self, filename: str, parent_path: str = "", match: MatchResult | None = None,
    ) -> ReleaseParseResult:
        result = self.delegate.parse_media(
            filename, parent_path, match or dataclasses.replace(self.fixed_match)
        )
        position = self._position_override(filename, parent_path)
        effective_season, effective_episode = (
            position
            if position is not None
            else (result.effective_season, result.effective_episode)
        )
        effective_season, effective_episode = self._apply_manual_position(
            effective_season, effective_episode
        )
        return dataclasses.replace(
            result,
            effective_season=effective_season,
            effective_episode=effective_episode,
        )

    def parse_source_position(
        self, filename: str, parent_path: str = ""
    ) -> tuple[int | None, int | None]:
        parsed = self.parse_media(filename, parent_path)
        return parsed.source_season, parsed.source_episode

    def parse_existing_media(self, filename: str) -> ReleaseParseResult:
        """目标目录已有文件按真实文件名解析，不套用本次手工覆盖。"""
        return self.delegate.parse_media(filename)

    def get_detail(self, tmdb_id: str, media_type: str) -> dict:
        if str(tmdb_id) == str(self.fixed_match.tmdb_id):
            return dict(self.detail)
        return self.delegate.get_detail(tmdb_id, media_type)


class ScopedGuangYaClient:
    # 作用域过滤是一次性的自顶向下扫描窗口：逐媒体组枚举会先消耗掉该窗口，
    # 导致后续扫描看到未过滤内容并移动范围外文件，因此禁用媒体组流水线。
    supports_group_pipeline = False

    def __init__(
        self,
        client,
        source_parent_id: str,
        allowed_file_ids: set[str],
        *,
        recursive: bool = False,
    ) -> None:
        self._client = client
        self._source_parent_id = str(source_parent_id)
        self._allowed_file_ids = {str(file_id) for file_id in allowed_file_ids}
        self._recursive = bool(recursive)
        self._source_scan_pending = False
        self._source_tree_ids: set[str] = set()
        self._filtered_tree_ids: set[str] = set()

    def begin_source_scan(self) -> None:
        self._source_scan_pending = True
        self._source_tree_ids = {self._source_parent_id}
        self._filtered_tree_ids = set()

    def list_dir(self, parent_id: str):
        items = self._client.list_dir(parent_id)
        current_id = str(parent_id)
        if not self._source_scan_pending or current_id not in self._source_tree_ids:
            return items
        if current_id in self._filtered_tree_ids:
            return items
        self._filtered_tree_ids.add(current_id)
        if not self._recursive:
            self._source_scan_pending = False
            return [
                item
                for item in items
                if str(item.file_id) in self._allowed_file_ids
            ]
        directories = [item for item in items if item.is_dir]
        self._source_tree_ids.update(str(item.file_id) for item in directories)
        return [
            item
            for item in items
            if item.is_dir or str(item.file_id) in self._allowed_file_ids
        ]

    def __getattr__(self, name):
        return getattr(self._client, name)


class PreviewSnapshotGuangYaClient:
    """在一次预览内复用刚完成的目录检查快照。

    预览仍先重新检查并校验 fingerprint；校验通过后，Organizer 的第二次
    源目录扫描直接读取同一只读快照。归档目标等源目录以外的读取继续委托
    给真实客户端，因此冲突检查与执行前目标快照语义保持不变。
    """

    def __init__(self, client, inspection: DirectoryInspection) -> None:
        self._client = client
        self._listings: dict[str, list[GuangYaFile]] = {
            str(inspection.directory_id): []
        }
        self._infos: dict[str, GuangYaFile] = {}
        for item in inspection.directories:
            cloud = GuangYaFile(
                str(item.file_id), str(item.name), True, 0,
                str(item.etag or ""), str(item.parent_id or "0"),
            )
            self._infos[cloud.file_id] = cloud
            self._listings.setdefault(cloud.file_id, [])
        for item in inspection.directories:
            file_id = str(item.file_id)
            if file_id == str(inspection.directory_id):
                continue
            parent_id = str(item.parent_id or "0")
            if parent_id in self._listings:
                self._listings[parent_id].append(self._infos[file_id])
        for item in (*inspection.videos, *inspection.companions):
            cloud = GuangYaFile(
                str(item.file_id), str(item.name), False, int(item.size or 0),
                str(item.etag or ""), str(item.parent_id or "0"),
            )
            self._infos[cloud.file_id] = cloud
            self._listings.setdefault(cloud.parent_id, []).append(cloud)

    def list_dir(self, parent_id: str):
        key = str(parent_id)
        if key in self._listings:
            return list(self._listings[key])
        return self._client.list_dir(parent_id)

    def file_info(self, file_id: str):
        return self._infos.get(str(file_id)) or self._client.file_info(file_id)

    def __getattr__(self, name):
        return getattr(self._client, name)


class DirectoryScrapeService:
    def __init__(
        self,
        client: GuangYaClient | None = None,
        scraper: TMDBScraper | None = None,
        store: DirectoryScrapeStore | None = None,
        rules_loader: Callable[[], OrganizeRules] = OrganizeRules.from_config,
    ) -> None:
        self.client = client or GuangYaClient()
        self.scraper = scraper or TMDBScraper()
        self.store = store or get_directory_scrape_store()
        self.rules_loader = rules_loader
        self._cached_nsfw_key: tuple[str, str, str, int] | None = None
        self._cached_nsfw_recognizer = None

    def _nsfw_recognizer(self, rules: OrganizeRules):
        if not rules.nsfw_enabled or not str(rules.nsfw_metatube_endpoint or "").strip():
            return None
        key = (
            str(rules.nsfw_metatube_endpoint or "").strip(),
            str(rules.nsfw_metatube_token or ""),
            str(rules.nsfw_strip_domains or ""),
            int(rules.nsfw_timeout_seconds or 8),
        )
        if self._cached_nsfw_key == key and self._cached_nsfw_recognizer is not None:
            return self._cached_nsfw_recognizer
        from app.modules.nsfw import NsfwRecognizer
        try:
            recognizer = NsfwRecognizer(
                key[0], key[1], strip_domains=key[2], timeout=key[3],
            )
        except ValueError:
            return None
        self._cached_nsfw_key = key
        self._cached_nsfw_recognizer = recognizer
        return recognizer

    @staticmethod
    def _validate_metatube_source_identity(
        inspection: DirectoryInspection,
        detail: dict,
        rules: OrganizeRules,
    ) -> None:
        from app.modules.nsfw import extract_nsfw_identifier, normalize_code

        source_codes: set[str] = set()
        source_values = [inspection.directory_name]
        for video in inspection.videos:
            source_values.extend((video.name, video.relative_dir))
        for value in source_values:
            identifier = extract_nsfw_identifier(value, rules.nsfw_strip_domains)
            if identifier is not None:
                source_codes.add(normalize_code(identifier.code))
        resolved_code = normalize_code(str(detail.get("number") or ""))
        if not source_codes:
            raise DirectoryScrapeRequestError(
                "当前目录未提取到可校验番号，不能直接套用成人元数据"
            )
        if not resolved_code or resolved_code not in source_codes:
            raise DirectoryScrapeRequestError("所选 MetaTube 条目番号与当前目录不一致")

    def _recognize(self, filename: str, parent_path: str, rules: OrganizeRules) -> MatchResult:
        recognizer = self._nsfw_recognizer(rules)
        if recognizer is not None:
            match = recognizer.match(filename, parent_path)
            if match is not None:
                return match
        return self.scraper.match(filename, parent_path)

    def inspect(self, owner: str, directory_id: str) -> dict:
        rules = self.rules_loader()
        inspection = DirectoryMediaInspector(
            client=self.client,
            scraper=self.scraper,
        ).inspect(directory_id, rules)
        inspection_id = self.store.put_inspection(
            owner,
            inspection,
            rules,
            scope_type="directory",
            scope_id=str(directory_id),
        )
        return self._inspection_payload(inspection_id, inspection, rules)

    def inspect_file(self, owner: str, file_id: str) -> dict:
        rules = self.rules_loader()
        inspection = DirectoryMediaInspector(
            client=self.client,
            scraper=self.scraper,
        ).inspect_file(file_id, rules)
        inspection_id = self.store.put_inspection(
            owner,
            inspection,
            rules,
            scope_type="file",
            scope_id=str(file_id),
        )
        return self._inspection_payload(inspection_id, inspection, rules)

    @staticmethod
    def _normalize_episode_overrides(
        record: InspectionRecord,
        media_type: str,
        season: int | None,
        episode: int | None,
    ) -> tuple[int | None, int | None]:
        if media_type != "tv":
            return None, None
        for label, value, minimum, maximum in (
            ("季号", season, 0, 99),
            ("集号", episode, 1, 999),
        ):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise DirectoryScrapeRequestError(f"{label}必须是整数")
            if not minimum <= value <= maximum:
                raise DirectoryScrapeRequestError(f"{label}超出允许范围")
        if record.scope_type == "directory":
            if episode is not None:
                raise DirectoryScrapeRequestError("目录刮削只能统一指定季号，不能覆盖全部集号")
            resolved_season = season if season is not None else record.inspection.season
            return resolved_season, None
        if record.scope_type != "file":
            raise DirectoryScrapeRequestError("刮削作用域无效，请重新检查")
        has_explicit_override = season is not None or episode is not None
        if not has_explicit_override:
            # 文件检查结果只是解析线索，不是用户确认的位置。若在这里把
            # S01E00、07.5 等检查结果重新注入为 manual override，会覆盖
            # Organizer 的 Season 00 自动映射。普通文件仍会由统一解析器
            # 恢复原季集，因此这里只保留真正由用户填写的覆盖值。
            return None, None
        resolved_episode = episode if episode is not None else record.inspection.episode
        resolved_season = season if season is not None else record.inspection.season
        if has_explicit_override and resolved_season is not None and resolved_episode is None:
            raise DirectoryScrapeRequestError("请同时指定集号")
        if resolved_episode is not None and resolved_season is None:
            resolved_season = 1
        return resolved_season, resolved_episode

    def _inspection_payload(
        self,
        inspection_id: str,
        inspection: DirectoryInspection,
        rules: OrganizeRules,
    ) -> dict:
        target = self.client.file_info(rules.target_dir_id)
        return {
            "inspection_id": inspection_id,
            "directory": {
                "id": inspection.directory_id,
                "name": inspection.directory_name,
            },
            "media_type": inspection.media_type,
            "suggested_query": inspection.suggested_query,
            "season": inspection.season,
            "episode": inspection.episode,
            "season_inferred": inspection.season_inferred,
            "requires_manual_match": inspection.requires_manual_match,
            "manual_match_reason": inspection.manual_match_reason,
            "counts": dict(inspection.counts),
            "pending_videos": [
                {
                    "file_id": item.file.file_id,
                    "name": item.file.name,
                    "relative_dir": item.file.relative_dir,
                    "reason": item.reason,
                }
                for item in inspection.pending_videos
            ],
            "archive_target": {
                "id": rules.target_dir_id,
                "name": target.name if target is not None else rules.target_dir_id,
            },
            "rules_summary": {
                "rename_enabled": rules.rename_enabled,
                "conflict_strategy": rules.conflict_strategy,
                "keep_multi_versions": rules.keep_multi_versions,
                "link_strm": rules.link_strm,
                "emby_refresh": rules.emby_refresh,
            },
        }

    def search(
        self,
        owner: str,
        inspection_id: str,
        query: str,
        media_type: str,
        year: str = "",
    ) -> list[dict]:
        record = self.store.get_inspection(owner, inspection_id)
        search_query = str(query or record.inspection.suggested_query).strip()
        if not search_query:
            raise DirectoryScrapeRequestError("请输入 TMDB 搜索词")
        normalized_type = str(media_type or "auto").strip().lower()
        if normalized_type not in {"auto", "movie", "tv"}:
            raise DirectoryScrapeRequestError("媒体类型只能是自动、电影或剧集")
        types = (
            (record.inspection.media_type,)
            if normalized_type == "auto"
            else (normalized_type,)
        )
        candidates: list[dict] = []
        seen: set[tuple[str, str, str]] = set()
        for current_type in types:
            for candidate in self.scraper.search_candidates(
                search_query,
                str(year or "").strip(),
                current_type,
            ):
                key = ("tmdb", str(candidate.tmdb_id), current_type)
                if not candidate.tmdb_id or key in seen:
                    continue
                seen.add(key)
                candidates.append(self._candidate_payload(candidate, current_type))
        if normalized_type in {"auto", "movie"}:
            recognizer = self._nsfw_recognizer(record.rules)
            if recognizer is not None:
                for candidate in recognizer.candidates(search_query):
                    key = (
                        str(candidate.provider or "metatube"),
                        str(candidate.external_id or ""),
                        "movie",
                    )
                    if not candidate.external_id or key in seen:
                        continue
                    seen.add(key)
                    candidates.append(self._candidate_payload(candidate, "movie"))
        candidates.sort(
            key=lambda item: (
                0 if item.get("provider") == "metatube" and float(item.get("score") or 0) >= 1 else 1,
                -float(item.get("score") or 0),
                str(item.get("title") or "").casefold(),
                str(item.get("external_id") or item.get("tmdb_id") or ""),
            )
        )
        return candidates

    def external_hints(
        self,
        owner: str,
        inspection_id: str,
        query: str,
        media_type: str,
    ) -> dict:
        """按需查询豆瓣/Bangumi 线索；结果不参与自动评分，也不写入映射。"""
        record = self.store.get_inspection(owner, inspection_id)
        search_query = str(query or record.inspection.suggested_query).strip()
        if not search_query:
            raise DirectoryScrapeRequestError("请输入外部资料搜索词")
        normalized_type = str(media_type or "auto").strip().lower()
        if normalized_type not in {"auto", "movie", "tv"}:
            raise DirectoryScrapeRequestError("媒体类型只能是自动、电影或剧集")
        resolved_type = (
            record.inspection.media_type
            if normalized_type == "auto"
            else normalized_type
        )
        from app.modules.recognition_hints import enabled_hint_providers

        providers = list(enabled_hint_providers(resolved_type))
        if not providers:
            return {
                "query": search_query,
                "items": [],
                "errors": [{
                    "provider": "external_hints",
                    "code": "disabled",
                    "message": "整理规则中的豆瓣/Bangumi 辅助识别均未开启",
                    "retry_after": 0,
                }],
                "providers_attempted": [],
                "providers_succeeded": [],
                "advisory": "启用后会在普通 TMDB 失败时辅助自动整理；人工线索仍需 TMDB 复核",
            }

        from app.discovery.search import get_discovery_search_service

        try:
            result = get_discovery_search_service().search(
                search_query,
                1,
                providers,
                timeout_seconds=5.0,
            )
        except ValueError as exc:
            raise DirectoryScrapeRequestError(str(exc)) from exc

        items = []
        for card in result.items:
            if resolved_type in {"movie", "tv"} and card.media_type != resolved_type:
                continue
            items.append({
                "provider": card.provider,
                "external_id": card.external_id,
                "media_type": card.media_type,
                "title": card.title,
                "original_title": card.original_title,
                "year": card.year,
                "rating": card.rating,
                "rating_source": card.rating_source,
                "overview": card.overview,
            })
            if len(items) >= 12:
                break
        return {
            "query": result.query,
            "items": items,
            "errors": list(result.errors),
            "providers_attempted": list(result.providers_attempted),
            "providers_succeeded": list(result.providers_succeeded),
            "advisory": "外部线索主要用于自动整理失败后的第二轮 TMDB 查询；不会直接写入 TMDB ID 或锁定",
        }

    def preview(
        self,
        owner: str,
        inspection_id: str,
        tmdb_id: str,
        media_type: str,
        *,
        provider: str = "tmdb",
        external_id: str = "",
        season: int | None = None,
        episode: int | None = None,
        numbering_mode: str = "auto",
    ) -> dict:
        record = self.store.get_inspection(owner, inspection_id)
        if media_type not in {"movie", "tv"}:
            raise DirectoryScrapeRequestError("媒体类型只能是电影或剧集")
        normalized_type = media_type
        raw_numbering_mode = str(numbering_mode or "auto").strip().lower()
        if raw_numbering_mode not in NUMBERING_MODES:
            raise DirectoryScrapeRequestError("剧集编号模式无效")
        normalized_numbering_mode = normalize_numbering_mode(raw_numbering_mode)
        season_override, episode_override = self._normalize_episode_overrides(
            record,
            normalized_type,
            season,
            episode,
        )
        try:
            current = self._inspect_scope(
                record.scope_type,
                record.scope_id,
                record.rules,
            )
        except ValueError as exc:
            raise DirectoryScrapeConflictError("目录内容已变化，请重新检查") from exc
        if current.fingerprint != record.inspection.fingerprint:
            raise DirectoryScrapeConflictError("目录内容已变化，请重新检查")
        resolved_provider = str(provider or "tmdb").strip().lower()
        requested_tmdb_id = str(tmdb_id or "").strip()
        if resolved_provider == "metatube":
            recognizer = self._nsfw_recognizer(record.rules)
            if recognizer is None:
                raise DirectoryScrapeRequestError("MetaTube 成人内容识别未启用或配置无效")
            try:
                match, detail = recognizer.resolve(str(external_id or ""))
            except Exception as exc:
                raise DirectoryScrapeRequestError(str(exc) or "MetaTube 详情不存在或无法确认") from exc
            self._validate_metatube_source_identity(current, detail, record.rules)
            normalized_type = "movie"
        else:
            # 先取含演职员的完整详情；真实 TMDBScraper 随后的 match_from_tmdb
            # 会直接复用同一详情，避免一次候选点击连续发出两次详情请求。
            detail = self.scraper.get_detail_with_credits(requested_tmdb_id, normalized_type)
            if not detail:
                raise DirectoryScrapeRequestError("TMDB 详情不存在或无法确认")
            match = self.scraper.match_from_tmdb(requested_tmdb_id, normalized_type)
            if not match.tmdb_id or match.need_confirm:
                raise DirectoryScrapeRequestError("TMDB 详情不存在或无法确认")
        season_detail_loader = getattr(self.scraper, "get_tv_season_detail", None)
        position_overrides, episode_mappings = self._mapped_position_overrides(
            current,
            detail,
            normalized_numbering_mode,
            season_override=season_override,
            episode_override=episode_override,
            season_detail_loader=(
                season_detail_loader if callable(season_detail_loader) else None
            ),
        )
        organizer = Organizer(
            client=PreviewSnapshotGuangYaClient(self.client, current),
            scraper=FixedMatchScraper(
                self.scraper,
                match,
                detail,
                season_override=season_override,
                episode_override=episode_override,
                preserve_specials=record.scope_type == "directory",
                position_overrides=position_overrides,
            ),
        )
        plans, stats = organizer.organize(
            record.inspection.directory_id,
            record.rules,
            dry_run=True,
            post_actions=False,
            # 预览负责在线探测并写入探测缓存：既让用户看到真实的动态范围、
            # 位深、帧率与音频规格，也让随后只读缓存的执行阶段得到同一结果。
            media_probe_cache_only=False,
        )
        for plan in plans:
            mapping = episode_mappings.get(str(plan.file_id))
            if mapping is not None:
                plan.source_season = mapping.source_season
                plan.source_episode = mapping.source_episode
                plan.episode_mapping = mapping
        self._apply_pending_stats(stats, current)
        companion_plans = self._companion_plans(current, plans, organizer)
        signature = self._plan_signature(plans, companion_plans)
        target_snapshot = self._target_snapshot(plans, record.rules, organizer)
        target = self.client.file_info(record.rules.target_dir_id)
        payload = {
            "cloud_write": False,
            "inspection_id": inspection_id,
            "directory": {
                "id": record.inspection.directory_id,
                "name": record.inspection.directory_name,
            },
            "counts": dict(record.inspection.counts),
            "pending_videos": [
                {
                    "file_id": item.file.file_id,
                    "name": item.file.name,
                    "relative_dir": item.file.relative_dir,
                    "reason": item.reason,
                }
                for item in record.inspection.pending_videos
            ],
            "archive_target": {
                "id": record.rules.target_dir_id,
                "name": target.name if target is not None else record.rules.target_dir_id,
            },
            "match": self._match_payload(match, detail),
            "plans": [self._plan_payload(plan) for plan in plans],
            "companion_plans": companion_plans,
            "stats": dict(stats),
            "rules": asdict(record.rules),
            "numbering": {
                "mode": normalized_numbering_mode,
                "changed": sum(1 for item in episode_mappings.values() if item.changed),
            },
        }
        preview_id = self.store.put_preview(
            owner,
            inspection_id,
            record.inspection,
            record.rules,
            match,
            detail,
            plans,
            companion_plans,
            signature,
            target_snapshot,
            stats,
            payload,
            scope_type=record.scope_type,
            scope_id=record.scope_id,
            season_override=season_override,
            episode_override=episode_override,
            numbering_mode=normalized_numbering_mode,
            position_overrides=position_overrides,
        )
        payload["preview_id"] = preview_id
        stored = self.store.get_preview(owner, preview_id)
        stored.payload["preview_id"] = preview_id
        return payload

    def auto_match(self, owner: str, inspection_id: str) -> dict:
        record = self.store.get_inspection(owner, inspection_id)
        if record.inspection.requires_manual_match:
            return {
                "status": "requires_manual",
                "inspection_id": inspection_id,
                "suggested_query": record.inspection.suggested_query,
                "media_type": record.inspection.media_type,
                "message": record.inspection.manual_match_reason,
                "candidates": [],
            }
        representative = record.inspection.representative
        recognition_name = representative.name
        if record.inspection.media_type == "tv" and record.inspection.suggested_query:
            ext = representative.name.rsplit(".", 1)[-1] if "." in representative.name else "mkv"
            recognition_name = f"{record.inspection.suggested_query}.S01E01.{ext}"
        match = self._recognize(
            recognition_name, record.inspection.directory_name, record.rules
        )
        has_identity = bool(
            match.tmdb_id or (getattr(match, "provider", "") and getattr(match, "external_id", ""))
        )
        if (
            not has_identity
            or match.need_confirm
            or match.media_type != record.inspection.media_type
        ):
            candidates = [
                self._candidate_payload(
                    candidate,
                    "tv" if candidate.media_type == "tv" else "movie",
                )
                for candidate in list(match.candidates or [])
                if candidate.tmdb_id or candidate.external_id
            ]
            return {
                "status": "requires_manual",
                "inspection_id": inspection_id,
                "suggested_query": record.inspection.suggested_query,
                "media_type": record.inspection.media_type,
                "message": (
                    "自动匹配类型与目录检测类型不一致，需要人工确认"
                    if has_identity and match.media_type != record.inspection.media_type
                    else match.error or "自动匹配结果需要人工确认"
                ),
                "candidates": candidates,
            }
        preview = self.preview(
            owner, inspection_id, match.tmdb_id, match.media_type,
            provider=str(getattr(match, "provider", "") or "tmdb"),
            external_id=str(getattr(match, "external_id", "") or ""),
        )
        return {"status": "matched", **preview}

    def preview_reference(self, owner: str, preview_id: str) -> str:
        return self.store.get_preview(owner, preview_id).inspection.directory_name

    def execute_preview(self, owner: str, preview_id: str) -> dict:
        record = self.store.claim_preview(owner, preview_id)
        try:
            current_rules = self.rules_loader()
            if asdict(current_rules) != asdict(record.rules):
                raise DirectoryScrapeConflictError("光鸭整理规则已变化，请重新检查并生成预览")
            try:
                current = self._inspect_scope(
                    record.scope_type,
                    record.scope_id,
                    current_rules,
                )
            except ValueError as exc:
                raise DirectoryScrapeConflictError("目录内容已变化，请重新检查并生成预览") from exc
            if current.fingerprint != record.inspection.fingerprint:
                raise DirectoryScrapeConflictError("目录内容已变化，请重新检查并生成预览")
            existing_ids = {
                int(row["id"])
                for row in db.list_organize_logs(limit=5000)
            }
            organizer = self._organizer(
                record.scope_type,
                current,
                FixedMatchScraper(
                    self.scraper,
                    record.match,
                    record.detail,
                    season_override=record.season_override,
                    episode_override=record.episode_override,
                    preserve_specials=record.scope_type == "directory",
                    position_overrides=record.position_overrides,
                ),
            )
            if self._target_snapshot(record.plans, record.rules, organizer) != record.target_snapshot:
                raise DirectoryScrapeConflictError("归档目标内容已变化，请重新检查并确认")
            signature_organizer = Organizer(
                client=PreviewSnapshotGuangYaClient(organizer.client, current),
                scraper=organizer.scraper,
            )
            current_plans, _current_stats = signature_organizer.organize(
                record.inspection.directory_id,
                record.rules,
                dry_run=True,
                post_actions=False,
            )
            current_companions = self._companion_plans(
                current, current_plans, signature_organizer,
            )
            if self._plan_signature(current_plans, current_companions) != record.signature:
                raise DirectoryScrapeConflictError("归档计划已变化，请重新检查并确认")
            self._begin_source_scan(organizer)
            _plans, stats = organizer.organize(
                record.inspection.directory_id,
                record.rules,
                dry_run=False,
                post_actions=False,
                source_name=record.inspection.directory_name,
                require_complete_scan=True,
                # 执行阶段只读探测缓存，保证最终文件名与用户确认过的预览
                # 完全一致；缓存由预览阶段的在线探测负责预热。
                media_probe_cache_only=True,
            )
            self._apply_pending_stats(stats, current)
            self._cleanup_selected_source(record, current, stats)
            Organizer.notify_directory_results(
                stats,
                record.rules,
                source_name=record.inspection.directory_name,
            )
            Organizer.trigger_post_actions(
                stats,
                record.rules,
                source_name=record.inspection.directory_name,
            )
            file_ids = {item.file_id for item in record.inspection.videos}
            new_rows = [
                row
                for row in db.list_organize_logs(limit=5000)
                if int(row["id"]) not in existing_ids and str(row["file_id"]) in file_ids
            ]
            log_ids = [int(row["id"]) for row in new_rows]
            self._record_successful_manual_confirmations(record, new_rows)
            self.store.consume_preview(owner, preview_id)
            return {
                "preview_id": preview_id,
                "directory": record.inspection.directory_name,
                "stats": stats,
                "log_ids": sorted(log_ids),
            }
        except Exception:
            self.store.release_preview(owner, preview_id)
            raise

    def _record_successful_manual_confirmations(
        self,
        record: PreviewRecord,
        rows: list[object],
    ) -> None:
        """仅把真正写入成功的人工预览结果沉淀为后续稳定知识。"""
        selected_id = str(record.match.tmdb_id or "").strip()
        if (
            not selected_id
            or record.match.media_type not in {"movie", "tv"}
            or str(record.match.provider or "tmdb").strip().lower() not in {"", "tmdb"}
        ):
            return
        confirm = getattr(self.scraper, "confirm", None)
        if not callable(confirm):
            return
        successful_ids = {
            str(row["file_id"])
            for row in rows
            if str(row["status"] or "").strip().lower() == "success"
        }
        if not successful_ids:
            return
        rejected_ids = sorted({
            str(candidate.tmdb_id or "").strip()
            for candidate in record.match.candidates or []
            if str(candidate.tmdb_id or "").strip()
            and str(candidate.tmdb_id or "").strip() != selected_id
        })
        for item in record.inspection.videos:
            if str(item.file_id) not in successful_ids:
                continue
            parent_path = "/".join(
                value
                for value in (record.inspection.directory_name, item.relative_dir)
                if value
            )
            try:
                confirm(
                    item.name,
                    selected_id,
                    record.match.title,
                    record.match.year,
                    record.match.media_type,
                    parent_path=parent_path,
                    rejected_tmdb_ids=rejected_ids,
                )
            except Exception as exc:
                # 学习属于附加收益，绝不能把已经完成的网盘移动回报成失败。
                logger.warning(
                    "人工刮削知识记录失败 file=%s type=%s",
                    item.file_id,
                    type(exc).__name__,
                )

    def _cleanup_selected_source(
        self,
        record: PreviewRecord,
        current: DirectoryInspection,
        stats: dict,
    ) -> None:
        """目录刮削完成后，复核并清理用户本次选择的空目录根。"""
        stats.setdefault("source_dir_cleaned", 0)
        stats.setdefault("empty_dir_cleanup_reasons", [])
        if record.scope_type != "directory" or not record.rules.clean_empty:
            return
        cleanup_unsafe = bool(
            stats.get("failed")
            or stats.get("scan_errors")
            or stats.get("stopped")
            or stats.get("replacement_cleanup_failed")
            or stats.get("audit_failures")
        )
        if cleanup_unsafe:
            stats["source_dir_cleanup_skipped"] = 1
            Organizer._append_reason(
                stats,
                "empty_dir_cleanup_reasons",
                "本次刮削存在失败或审计异常，已保留所选源目录用于核对",
                limit=6,
            )
            logger.warning("目录刮削存在失败或审计异常，已保留所选源目录用于核对")
            return
        source_id = str(record.scope_id or current.directory_id)
        configured_sources, config_error = normalize_organize_sources(
            config.get("GY_ORGANIZE_SOURCE_DIRS", "")
        )
        protected_ids = {
            "0",
            str(record.rules.target_dir_id or "").strip(),
            *(str(item.get("id") or "").strip() for item in configured_sources),
        }
        protected_ids.discard("")
        if config_error:
            stats["source_dir_cleanup_skipped"] = 1
            Organizer._append_reason(
                stats,
                "empty_dir_cleanup_reasons",
                "整理来源配置无法安全解析，已保留所选源目录",
                limit=6,
            )
            logger.warning(
                "永久整理源配置无效，目录刮削已保留所选源目录: %s",
                config_error,
            )
            return
        if source_id in protected_ids:
            stats["source_dir_cleanup_protected"] = 1
            Organizer._append_reason(
                stats,
                "empty_dir_cleanup_reasons",
                "配置的来源或归档根目录按安全策略保留，仅清理其中的空子目录",
                limit=6,
            )
            return
        try:
            remaining = self.client.list_dir(source_id)
            if remaining:
                stats["source_dir_cleanup_not_empty"] = len(remaining)
                Organizer._append_reason(
                    stats,
                    "empty_dir_cleanup_reasons",
                    f"所选源目录仍有 {len(remaining)} 个文件或子目录，已保留",
                    limit=6,
                )
                return
            source = self.client.file_info(source_id)
            delete_empty = getattr(self.client, "delete_empty_directory", None)
            explicit_capability = getattr(
                self.client, "supports_guarded_empty_directory_delete", None
            )
            if explicit_capability is None:
                explicit_capability = getattr(
                    self.client, "supports_atomic_empty_directory_delete", None
                )
            if source is None or not source.is_dir:
                stats["source_dir_cleanup_unavailable"] = 1
                raise RuntimeError("所选源目录状态已变化，已安全保留")
            if not callable(delete_empty) or explicit_capability is False:
                stats["source_dir_cleanup_unsupported"] = 1
                raise RuntimeError("云盘接口不支持安全清理所选源目录")
            expected_etag = str(getattr(source, "etag", "") or "")
            try:
                expected_updated_at = max(
                    0, int(getattr(source, "updated_at", 0) or 0)
                )
            except (TypeError, ValueError):
                expected_updated_at = 0
            if not expected_etag and not expected_updated_at:
                raise RuntimeError("所选源目录缺少可验证版本信息")
            execute_recycle_bin_delete(
                self.client,
                trigger="directory_scrape_source_cleanup",
                reason="目录刮削完成后清理本次选择的空源目录",
                candidate=DeleteCandidate(
                    file_id=source_id,
                    name=source.name or current.directory_name,
                    parent_id=str(source.parent_id or "0"),
                ),
                delete_operation=lambda: delete_empty(
                    source_id,
                    expected_etag=expected_etag,
                    expected_updated_at=expected_updated_at,
                ),
            )
            stats["source_dir_cleaned"] = 1
            stats["empty_dirs_cleaned"] = int(stats.get("empty_dirs_cleaned", 0) or 0) + 1
            logger.info("目录刮削已清理空源目录: %s", source_id)
        except Exception as exc:
            stats["source_dir_cleanup_failed"] = 1
            reason = " ".join(str(exc or type(exc).__name__).split())[:160]
            Organizer._append_reason(
                stats,
                "empty_dir_cleanup_reasons",
                reason or "所选源目录清理失败，已安全保留",
                limit=6,
            )
            logger.warning(
                "目录刮削清理空源目录失败 source=%s type=%s",
                source_id,
                type(exc).__name__,
            )


    def _inspect_scope(
        self,
        scope_type: str,
        scope_id: str,
        rules: OrganizeRules,
    ) -> DirectoryInspection:
        inspector = DirectoryMediaInspector(
            client=self.client,
            scraper=self.scraper,
        )
        if scope_type == "file":
            return inspector.inspect_file(scope_id, rules)
        if scope_type == "directory":
            return inspector.inspect(scope_id, rules)
        raise DirectoryScrapeConflictError("刮削作用域无效，请重新检查")

    @staticmethod
    def _begin_source_scan(organizer: Organizer) -> None:
        if isinstance(organizer.client, ScopedGuangYaClient):
            organizer.client.begin_source_scan()

    def _organizer(
        self,
        scope_type: str,
        inspection: DirectoryInspection,
        scraper,
    ) -> Organizer:
        client = self.client
        if scope_type == "file":
            allowed_file_ids = {
                item.file_id
                for item in (*inspection.videos, *inspection.companions)
            }
            client = ScopedGuangYaClient(
                self.client,
                inspection.directory_id,
                allowed_file_ids,
            )
        elif scope_type == "directory" and inspection.pending_videos:
            allowed_file_ids = {
                item.file_id
                for item in (*inspection.videos, *inspection.companions)
            }
            client = ScopedGuangYaClient(
                self.client,
                inspection.directory_id,
                allowed_file_ids,
                recursive=True,
            )
        elif scope_type != "directory":
            raise DirectoryScrapeConflictError("刮削作用域无效，请重新检查")
        return Organizer(client=client, scraper=scraper)

    @staticmethod
    def _apply_pending_stats(stats: dict, inspection: DirectoryInspection) -> None:
        pending = list(inspection.pending_videos)
        stats["pending_confirmation"] = len(pending)
        stats["pending_files"] = [
            {
                "file_id": item.file.file_id,
                "name": item.file.name,
                "relative_dir": item.file.relative_dir,
                "reason": item.reason,
            }
            for item in pending[:10]
        ]

    def _candidate_payload(self, candidate: Candidate, media_type: str) -> dict:
        provider = str(getattr(candidate, "provider", "") or "tmdb").lower()
        if provider == "metatube":
            detail = dict(getattr(candidate, "metadata", {}) or {})
            release_date = str(detail.get("release_date") or candidate.release_date or "")
            return {
                "provider": "metatube",
                "external_id": str(candidate.external_id or ""),
                "tmdb_id": "",
                "title": str(candidate.title or detail.get("title") or ""),
                "original_title": str(candidate.original_title or detail.get("title") or ""),
                "year": release_date[:4] or str(candidate.year or ""),
                "release_date": release_date,
                "media_type": "movie",
                "score": float(candidate.score or 0),
                "overview": str(detail.get("summary") or candidate.overview or ""),
                "poster_url": str(detail.get("big_cover_url") or detail.get("cover_url") or candidate.poster_path or ""),
                "backdrop_url": str(detail.get("big_thumb_url") or detail.get("thumb_url") or ""),
                "vote_average": detail.get("score"),
                "genres": [str(item) for item in detail.get("genres") or [] if str(item)],
                "director": [str(detail.get("director"))] if detail.get("director") else [],
                "creators": [],
                "cast": [str(item) for item in detail.get("actors") or [] if str(item)][:8],
                "homepage": str(detail.get("homepage") or ""),
                "number": str(detail.get("number") or ""),
            }
        # 搜索列表只使用 TMDB 搜索响应已有字段；完整详情在用户选中候选后
        # 由 preview() 获取，避免 Top 3 候选各自追加 credits 请求。
        release_date = str(candidate.release_date or "")
        return {
            "provider": "tmdb",
            "external_id": str(candidate.tmdb_id),
            "tmdb_id": str(candidate.tmdb_id),
            "title": str(candidate.title or ""),
            "original_title": str(candidate.original_title or ""),
            "year": release_date[:4] or str(candidate.year or ""),
            "release_date": release_date,
            "media_type": media_type,
            "score": float(candidate.score or 0),
            "overview": str(candidate.overview or ""),
            "poster_url": _tmdb_image_url(candidate.poster_path, "w342"),
            "backdrop_url": _tmdb_image_url(candidate.backdrop_path, "w780"),
            "vote_average": None,
            "genres": [],
            "director": [],
            "creators": [],
            "cast": [],
            "homepage": "",
        }

    @staticmethod
    def _position_overrides(
        inspection: DirectoryInspection,
    ) -> dict[object, tuple[int | None, int | None]]:
        counts: dict[str, int] = {}
        for item in inspection.videos:
            counts[item.name] = counts.get(item.name, 0) + 1
        result: dict[object, tuple[int | None, int | None]] = {}
        for item in inspection.videos:
            if item.season is None and item.episode is None:
                continue
            position = (item.season, item.episode)
            relative_parent = str(item.relative_dir or "").replace("\\", "/").strip("/")
            full_parent = "/".join(
                value for value in (inspection.directory_name, relative_parent) if value
            )
            result[(relative_parent, item.name)] = position
            result[(full_parent, item.name)] = position
            if counts.get(item.name) == 1:
                result[item.name] = position
        return result

    @classmethod
    def _mapped_position_overrides(
        cls,
        inspection: DirectoryInspection,
        detail: dict,
        numbering_mode: str,
        *,
        season_override: int | None = None,
        episode_override: int | None = None,
        season_detail_loader: Callable[[str, int], dict] | None = None,
    ) -> tuple[
        dict[object, tuple[int | None, int | None]],
        dict[str, EpisodeMappingPlan],
    ]:
        counts: dict[str, int] = {}
        for item in inspection.videos:
            counts[item.name] = counts.get(item.name, 0) + 1
        overrides: dict[object, tuple[int | None, int | None]] = {}
        mappings: dict[str, EpisodeMappingPlan] = {}
        mode = normalize_numbering_mode(numbering_mode)
        season_detail_cache: dict[tuple[str, int], dict] = {}
        evidence = build_directory_episode_evidence([
            (
                item.relative_dir or "__root__",
                "/".join(
                    value for value in (inspection.directory_name, item.relative_dir) if value
                ),
                season_override if season_override is not None and item.season != 0 else item.season,
                item.episode,
            )
            for item in inspection.videos
        ])
        for item in inspection.videos:
            source_season, source_episode = item.season, item.episode
            special = bool(
                source_season == 0
                or is_special_media_name(item.name)
                or is_special_path(item.relative_dir)
            )
            # SxxE00、小数集以及 Extras/Specials 目录中的媒体由 Organizer
            # 统一分配 Season 00 槽位。这里若把检查器解析出的 (1, 0)、
            # (1, 7) 再写成固定 position override，会覆盖自动特殊集映射，
            # 导致手动预览与自动整理结果不一致。只有用户明确填写“集”时，
            # 才把它视为人工位置覆盖。
            defer_special_position = special and episode_override is None
            effective_season = (
                season_override
                if season_override is not None and not special
                else source_season
            )
            parent_path = "/".join(
                value for value in (inspection.directory_name, item.relative_dir) if value
            )
            directory_evidence = evidence.get(item.relative_dir or "__root__")
            mapping = infer_episode_mapping(
                source_season=effective_season,
                source_episode=source_episode,
                parent_path=parent_path,
                detail=detail,
                mode=mode,
                directory_evidence=directory_evidence,
            )
            # 单季 TMDB 条目下，发布方的第二季若从 E01 重新编号，普通
            # absolute 回退会错误地保持 E01；但单个文件不足以证明它属于
            # split-cour。只有目录形成从 E01 开始、至少 3 集且无断档的完整
            # 连续证据时，才允许用播出间隔重映射。S02E13-E24 这类绝对集号
            # 包不会满足该条件，因此继续保留普通 absolute 映射。
            split_cour_directory_evidence = bool(
                directory_evidence is not None
                and directory_evidence.contiguous
                and directory_evidence.source_season == effective_season
                and directory_evidence.range_start == 1
                and directory_evidence.episode_count >= 3
                and directory_evidence.episode_count == directory_evidence.range_end
                and source_episode is not None
                and source_episode <= directory_evidence.range_end
            )
            split_cour_probe_needed = split_cour_directory_evidence and (
                not mapping.changed
                or (
                    mapping.mode == "absolute"
                    and mapping.target_episode == source_episode
                )
            )
            if (
                mode == "auto"
                and split_cour_probe_needed
                and effective_season is not None
                and effective_season >= 2
                and source_episode is not None
                and season_detail_loader is not None
            ):
                counts_by_season = season_episode_counts(detail)
                if len(counts_by_season) == 1 and effective_season not in counts_by_season:
                    merged_target_season = next(iter(counts_by_season))
                    tmdb_id = str(detail.get("id") or "").strip()
                    cache_key = (tmdb_id, merged_target_season)
                    if cache_key not in season_detail_cache:
                        try:
                            loaded = season_detail_loader(tmdb_id, merged_target_season)
                        except Exception:
                            loaded = {}
                        season_detail_cache[cache_key] = (
                            dict(loaded) if isinstance(loaded, dict) else {}
                        )
                    merged_mapping = infer_merged_season_cour_mapping(
                        source_season=effective_season,
                        source_episode=source_episode,
                        detail=detail,
                        season_detail=season_detail_cache[cache_key],
                    )
                    if merged_mapping.confidence >= 1.0:
                        mapping = merged_mapping
            target_season = mapping.target_season
            target_episode = mapping.target_episode
            if season_override is not None and not special:
                target_season = season_override
                if mapping.target_season != target_season:
                    mapping = dataclasses.replace(
                        mapping,
                        target_season=target_season,
                        reason="manual_season_override",
                        confidence=1.0,
                    )
            if episode_override is not None:
                target_episode = episode_override
                mapping = dataclasses.replace(
                    mapping,
                    target_episode=target_episode,
                    reason="manual_episode_override",
                    confidence=1.0,
                )
            if source_season is None and source_episode is None:
                continue
            if defer_special_position:
                continue
            position = (target_season, target_episode)
            relative_parent = str(item.relative_dir or "").replace("\\", "/").strip("/")
            full_parent = "/".join(
                value for value in (inspection.directory_name, relative_parent) if value
            )
            overrides[(relative_parent, item.name)] = position
            overrides[(full_parent, item.name)] = position
            if counts.get(item.name) == 1:
                overrides[item.name] = position
            mappings[str(item.file_id)] = dataclasses.replace(
                mapping,
                source_season=source_season,
                source_episode=source_episode,
            )
        return overrides, mappings

    @staticmethod
    def _match_payload(match: MatchResult, detail: dict) -> dict:
        release_date = str(
            detail.get("first_air_date") or detail.get("release_date") or ""
        )
        return {
            "tmdb_id": str(match.tmdb_id),
            "title": str(detail.get("name") or detail.get("title") or match.title or ""),
            "original_title": str(
                detail.get("original_name") or detail.get("original_title") or ""
            ),
            "year": release_date[:4] or str(match.year or ""),
            "release_date": release_date,
            "media_type": "tv" if match.media_type == "tv" else "movie",
            "overview": str(detail.get("overview") or ""),
            "vote_average": detail.get("vote_average"),
            "genres": _unique_names(detail.get("genres")),
            "poster_url": _tmdb_image_url(detail.get("poster_path"), "w342"),
            "backdrop_url": _tmdb_image_url(detail.get("backdrop_path"), "w780"),
        }

    @staticmethod
    def _plan_payload(plan: OrganizePlan) -> dict:
        return {
            "file_id": plan.file_id,
            "action": plan.action,
            "original_name": plan.original_name,
            "original_path": plan.original_path,
            "new_name": plan.new_name,
            "target_path": plan.target_path,
            "variant_label": plan.variant_label,
            "conflict_decision": plan.conflict_decision,
            "conflict_note": plan.conflict_note,
            "note": plan.note,
            "source_season": plan.source_season,
            "source_episode": plan.source_episode,
            "season": plan.season,
            "episode": plan.episode,
            "episode_mapping": (
                plan.episode_mapping.to_dict() if plan.episode_mapping is not None else None
            ),
        }

    @staticmethod
    def _as_cloud_file(item) -> GuangYaFile:
        return GuangYaFile(
            item.file_id, item.name, False, item.size, item.etag, item.parent_id
        )

    def _companion_plans(
        self,
        inspection: DirectoryInspection,
        plans: list[OrganizePlan],
        organizer: Organizer,
    ) -> list[dict]:
        videos_by_dir: dict[str, list[GuangYaFile]] = {}
        companions_by_dir: dict[str, list[GuangYaFile]] = {}
        snapshot_by_id = {}
        for item in inspection.videos:
            cloud = self._as_cloud_file(item)
            videos_by_dir.setdefault(item.relative_dir, []).append(cloud)
            snapshot_by_id[item.file_id] = item
        for item in inspection.companions:
            cloud = self._as_cloud_file(item)
            companions_by_dir.setdefault(item.relative_dir, []).append(cloud)
            snapshot_by_id[item.file_id] = item
        plan_by_id = {plan.file_id: plan for plan in plans}
        result: list[dict] = []
        assigned: set[str] = set()
        for relative_dir, companions in companions_by_dir.items():
            subtitles = [
                item for item in companions
                if media_role(item.name) == "subtitle"
            ]
            subtitle_result = plan_subtitle_companions(
                videos_by_dir.get(relative_dir, []), subtitles
            )
            for subtitle_plan in subtitle_result.plans:
                plan = plan_by_id.get(subtitle_plan.video_file_id)
                if not plan:
                    continue
                assigned.add(subtitle_plan.file.file_id)
                target_name = subtitle_plan.target_name(plan.new_name or plan.original_name)
                snap = snapshot_by_id[subtitle_plan.file.file_id]
                result.append({
                    "file_id": snap.file_id, "role": "subtitle",
                    "original_name": snap.name, "relative_dir": snap.relative_dir,
                    "video_file_id": plan.file_id, "action": plan.action,
                    "target_name": target_name, "note": plan.note,
                })
            for skipped in subtitle_result.skipped:
                assigned.add(skipped.file.file_id)
                snap = snapshot_by_id[skipped.file.file_id]
                result.append({
                    "file_id": snap.file_id, "role": "subtitle",
                    "original_name": snap.name, "relative_dir": snap.relative_dir,
                    "video_file_id": "", "action": "skip",
                    "target_name": "", "note": skipped.reason,
                })
            non_subtitles = [
                item for item in companions
                if media_role(item.name) != "subtitle"
            ]
            for plan in plans:
                if plan.original_path != relative_dir:
                    continue
                for companion in organizer._companions_for_plan(plan, non_subtitles):
                    if companion.file_id in assigned:
                        continue
                    assigned.add(companion.file_id)
                    snap = snapshot_by_id[companion.file_id]
                    result.append({
                        "file_id": snap.file_id,
                        "role": media_role(snap.name),
                        "original_name": snap.name,
                        "relative_dir": snap.relative_dir,
                        "video_file_id": plan.file_id,
                        "action": plan.action,
                        "target_name": companion_target_name(
                            plan.original_name, plan.new_name or plan.original_name, snap.name
                        ),
                        "note": plan.note,
                    })
        return sorted(result, key=lambda item: item["file_id"])

    def _plan_signature(
        self, plans: list[OrganizePlan], companion_plans: list[dict]
    ) -> tuple:
        video = tuple(sorted(
            (
                plan.file_id, plan.original_parent_id, plan.original_name,
                plan.action, plan.target_path, plan.new_name,
                plan.conflict_decision, plan.conflict_note,
            )
            for plan in plans
        ))
        companions = tuple(sorted(
            (
                item["file_id"], item["video_file_id"], item["action"],
                item["target_name"], item["note"],
            )
            for item in companion_plans
        ))
        return video, companions

    def _target_snapshot(
        self,
        plans: list[OrganizePlan],
        rules: OrganizeRules,
        organizer: Organizer,
    ) -> tuple:
        snapshot = []
        for path in sorted({plan.target_path for plan in plans if plan.target_path}):
            target_id = organizer._find_existing_dir_chain(rules.target_dir_id, path)
            if not target_id:
                snapshot.append((path, "", ()))
                continue
            files = tuple(sorted(
                (
                    str(item.file_id), str(item.parent_id or target_id), str(item.name),
                    bool(item.is_dir), int(item.size or 0), str(item.etag or ""),
                )
                for item in self.client.list_dir(target_id)
            ))
            snapshot.append((path, str(target_id), files))
        return tuple(snapshot)


_store = DirectoryScrapeStore()


def get_directory_scrape_store() -> DirectoryScrapeStore:
    return _store


_service: DirectoryScrapeService | None = None
_service_lock = threading.Lock()


def get_directory_scrape_service() -> DirectoryScrapeService:
    global _service
    with _service_lock:
        if _service is None:
            _service = DirectoryScrapeService(store=_store)
        return _service
