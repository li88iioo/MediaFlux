"""本地媒体检查、识别预览和移动编排服务。"""
from __future__ import annotations

import inspect
import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from app import database as db
from app.clients.guangya import GuangYaFile
from app.modules.local_move_transaction import LocalMoveTransaction, MoveTransactionResult
from app.modules.local_path_mapping import assert_within, validate_source_target_roots
from app.modules.local_storage import (
    LocalContentChanged, LocalFileSnapshot, LocalFilesystemAdapter, snapshot_digest,
)
from app.modules.local_media_cleanup import delete_cleanup_items, discover_cleanup_candidates
from app.modules.organize import (
    OrganizeRules,
    Organizer,
    automatic_match_requires_confirmation,
    enforce_fixed_organize_rules,
)
from app.modules.media_probe import ProbeBudget, probe_local_media_profile
from app.modules.recognition_policy import (
    automatic_match_confirmation_message,
    automatic_match_policy,
)
from app.modules.scraper import MatchResult, TMDBScraper
from app.config import get, get_bool
from app.modules.subtitle_identity import plan_subtitle_companions

_SERVER_ONLY_RULE_FIELDS = {
    "nsfw_enabled",
    "nsfw_metatube_endpoint",
    "nsfw_metatube_token",
    "nsfw_category_name",
    "nsfw_strip_domains",
    "nsfw_timeout_seconds",
}


class LocalMediaServiceError(RuntimeError):
    """可安全显示的本地媒体业务错误。"""


@dataclass(frozen=True)
class LocalMovePlan:
    source: LocalFileSnapshot
    target: Path
    role: str
    media_group: str
    action: str = "move"
    note: str = ""
    provider: str = ""
    library_id: str = ""
    library_name: str = ""
    expected_target_identity: tuple[int, int, int, int] | None = None


@dataclass(frozen=True)
class _LocalFile:
    file_id: str
    name: str
    snapshot: LocalFileSnapshot


@dataclass
class _Inspection:
    owner: str
    source_id: int
    root: Path
    selected_path: Path
    snapshots: list[LocalFileSnapshot]
    digest: str
    created_at: float


class _InspectionStore:
    def __init__(self, ttl_seconds: int = 1800) -> None:
        self.ttl_seconds = ttl_seconds
        self._records: dict[str, _Inspection] = {}
        self._lock = threading.RLock()

    def put(self, record: _Inspection) -> str:
        inspection_id = uuid.uuid4().hex
        with self._lock:
            self._prune_locked()
            self._records[inspection_id] = record
        return inspection_id

    def get(self, owner: str, inspection_id: str) -> _Inspection:
        with self._lock:
            self._prune_locked()
            record = self._records.get(str(inspection_id))
            if record is None or record.owner != str(owner):
                raise LocalMediaServiceError("检查记录不存在或已过期")
            return record

    def _prune_locked(self) -> None:
        deadline = time.time() - self.ttl_seconds
        for key in [key for key, value in self._records.items() if value.created_at < deadline]:
            self._records.pop(key, None)


_CATEGORY_KEYS = {
    "电影": "movie",
    "剧集": "tv",
    "动漫": "anime",
    "纪录片": "documentary",
    "综艺": "variety",
    "演唱会": "concert",
    "儿童节目": "kids",
}


class LocalMediaService:
    def __init__(
        self,
        scraper: TMDBScraper | None = None,
        *,
        inspection_store: _InspectionStore | None = None,
    ) -> None:
        self.scraper = scraper or TMDBScraper()
        self.organizer = Organizer(client=object(), scraper=self.scraper)
        self.inspections = inspection_store or _InspectionStore()

    def inspect_source(self, owner: str, source_id: int, path: Path | str) -> dict[str, Any]:
        source = db.get_local_media_source(source_id, owner=owner)
        if source is None:
            raise LocalMediaServiceError("本地媒体来源不存在")
        root = Path(source.local_root).expanduser().absolute()
        assert_within(root, root)
        selected = assert_within(Path(path), root)
        snapshots = LocalFilesystemAdapter(root).scan(selected)
        videos = [item for item in snapshots if item.role == "video"]
        if not videos:
            raise LocalMediaServiceError("选择路径中没有可整理的视频")
        digest = snapshot_digest(snapshots)
        inspection_id = self.inspections.put(_Inspection(
            owner=str(owner), source_id=int(source_id), root=root, selected_path=selected,
            snapshots=snapshots, digest=digest, created_at=time.time(),
        ))
        return {
            "inspection_id": inspection_id,
            "source_id": int(source_id),
            "selected_name": selected.name,
            "selected_kind": "file" if selected.is_file() else "directory",
            "file_count": len(snapshots),
            "video_count": len(videos),
            "digest": digest,
            "cloud_write": False,
            "files": [
                {"name": item.path.name, "relative_path": item.relative_path, "role": item.role, "size": item.size}
                for item in snapshots
            ],
        }

    def inspect_task(self, owner: str, task_id: int) -> dict[str, Any]:
        task = db.get_local_media_task(task_id, owner=owner)
        if task is None:
            raise LocalMediaServiceError("本地媒体任务不存在")
        if task.status != "requires_manual":
            raise LocalMediaServiceError("仅待确认任务可以进入人工复核")
        return self.inspect_source(owner, task.source_id, task.content_path)

    def search(self, query: str, year: str = "", media_type: str = "movie") -> list[dict[str, Any]]:
        return [
            {
                "tmdb_id": item.tmdb_id, "title": item.title, "year": item.year,
                "media_type": item.media_type or media_type, "score": item.score,
                "overview": item.overview, "poster_path": item.poster_path,
            }
            for item in self.scraper.search_candidates(query, year, media_type)
        ]

    @staticmethod
    def _target_for_category(targets, category: str):
        by_category = {item.category: item for item in targets}
        return by_category.get(category) or by_category.get("default")

    @staticmethod
    def _relative_parent(snapshot: LocalFileSnapshot) -> str:
        parent = Path(snapshot.relative_path).parent.as_posix()
        return "" if parent == "." else parent

    @staticmethod
    def _normalize_position_overrides(
        inspection: _Inspection,
        season_override: int | None,
        episode_override: int | None,
    ) -> tuple[int | None, int | None]:
        for value, minimum, maximum, label in (
            (season_override, 0, 99, "季数"),
            (episode_override, 1, 999, "集数"),
        ):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise LocalMediaServiceError(f"{label}必须是整数")
            if not minimum <= value <= maximum:
                raise LocalMediaServiceError(f"{label}必须是 {minimum}-{maximum} 的整数")
        if episode_override is not None and not inspection.selected_path.is_file():
            raise LocalMediaServiceError("目录整理只能指定归档季，不能统一指定集数")
        if episode_override is not None and season_override is None:
            season_override = 1
        return season_override, episode_override

    @staticmethod
    def _confirmation_candidates(match: MatchResult) -> list[dict[str, Any]]:
        """把识别结果压缩成可持久化的安全候选，供 Web/TG 共用。"""
        raw_candidates = list(match.candidates or [])
        candidates: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        def append_candidate(item, *, primary: bool = False) -> None:
            tmdb_id = str(getattr(item, "tmdb_id", "") or "").strip()
            media_type = str(
                getattr(item, "media_type", "") or match.media_type or ""
            ).strip().lower()
            provider = str(
                getattr(item, "provider", "") or match.provider or "tmdb"
            ).strip().lower()
            if not tmdb_id or media_type not in {"movie", "tv"} or provider != "tmdb":
                return
            key = (tmdb_id, media_type)
            if key in seen:
                return
            seen.add(key)
            metadata = getattr(item, "metadata", None)
            metadata = metadata if isinstance(metadata, dict) else {}
            raw_genres = metadata.get("genre_ids") or []
            candidates.append({
                "tmdb_id": tmdb_id,
                "title": str(getattr(item, "title", "") or match.title or "").strip(),
                "year": str(getattr(item, "year", "") or match.year or "").strip(),
                "media_type": media_type,
                "score": float(
                    match.confidence if primary else (getattr(item, "score", 0.0) or 0.0)
                ),
                "confidence": float(
                    match.confidence if primary else (getattr(item, "score", 0.0) or 0.0)
                ),
                "provider": provider,
                "external_id": str(
                    getattr(item, "external_id", "") or tmdb_id
                ).strip(),
                "genre_ids": [
                    int(value) for value in raw_genres if str(value).isdigit()
                ],
            })

        append_candidate(match, primary=True)
        for candidate in raw_candidates:
            append_candidate(candidate)
        return candidates[:3]

    def _match_video(
        self,
        video: LocalFileSnapshot,
        *,
        tmdb_id: str = "",
        media_type: str = "",
        rules: OrganizeRules | None = None,
    ) -> MatchResult:
        if tmdb_id:
            return self.scraper.match_from_tmdb(tmdb_id, media_type or "movie")
        if rules is not None and type(self.scraper) is TMDBScraper:
            recognizer = self.organizer._nsfw_recognizer(rules)
            if recognizer is not None:
                match = recognizer.match(video.path.name, self._relative_parent(video))
                if match is not None:
                    return match
        matcher = self.scraper.match
        try:
            parameters = inspect.signature(matcher).parameters
        except (TypeError, ValueError):
            parameters = {}
        if "media_type_hint" in parameters or any(
            item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values()
        ):
            return matcher(
                video.path.name, self._relative_parent(video), media_type_hint=media_type
            )
        return matcher(video.path.name, self._relative_parent(video))

    @staticmethod
    def _serialize_rules_snapshot(rules: OrganizeRules) -> str:
        payload = asdict(rules)
        for key in _SERVER_ONLY_RULE_FIELDS:
            payload.pop(key, None)
        payload["target_dir_id"] = "0"
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _restore_rules_snapshot(raw: str) -> OrganizeRules:
        try:
            payload = json.loads(str(raw or ""))
        except (TypeError, ValueError) as exc:
            raise LocalMediaServiceError("整理规则快照无效，请重新生成预览") from exc
        if not isinstance(payload, dict):
            raise LocalMediaServiceError("整理规则快照无效，请重新生成预览")
        fields = OrganizeRules.__dataclass_fields__
        if set(payload) - set(fields):
            raise LocalMediaServiceError("整理规则快照包含未知字段，请重新生成预览")
        baseline = OrganizeRules()
        values: dict[str, Any] = {}
        for key, value in payload.items():
            if key in _SERVER_ONLY_RULE_FIELDS:
                # 兼容修复前生成的快照，但绝不采信浏览器回传的运行时服务配置。
                continue
            current = getattr(baseline, key)
            if isinstance(current, bool):
                if not isinstance(value, bool):
                    raise LocalMediaServiceError("整理规则快照类型无效，请重新生成预览")
            elif isinstance(current, int):
                if not isinstance(value, int) or isinstance(value, bool):
                    raise LocalMediaServiceError("整理规则快照类型无效，请重新生成预览")
            elif not isinstance(value, str):
                raise LocalMediaServiceError("整理规则快照类型无效，请重新生成预览")
            values[key] = value
        trusted = OrganizeRules.from_config()
        for key in _SERVER_ONLY_RULE_FIELDS:
            values[key] = getattr(trusted, key)
        values["target_dir_id"] = "0"
        return enforce_fixed_organize_rules(OrganizeRules(**values))

    def preview(
        self,
        owner: str,
        inspection_id: str,
        tmdb_id: str = "",
        media_type: str = "",
        overrides: dict | None = None,
        rules_snapshot: str = "",
        automatic: bool = False,
        season_override: int | None = None,
        episode_override: int | None = None,
    ) -> dict[str, Any]:
        inspection = self.inspections.get(owner, inspection_id)
        season_override, episode_override = self._normalize_position_overrides(
            inspection, season_override, episode_override,
        )
        source = db.get_local_media_source(inspection.source_id, owner=owner)
        if source is None:
            raise LocalMediaServiceError("本地媒体来源已被删除")
        current = LocalFilesystemAdapter(inspection.root).scan(inspection.selected_path)
        if snapshot_digest(current) != inspection.digest:
            raise LocalMediaServiceError("源文件在检查后发生变化，请重新检查")
        targets = db.list_local_library_targets(inspection.source_id, owner=owner)
        if not targets:
            raise LocalMediaServiceError("当前来源尚未配置归档目标")
        validate_source_target_roots(inspection.root, [Path(item.path) for item in targets])

        if rules_snapshot:
            rules = self._restore_rules_snapshot(rules_snapshot)
        else:
            rules = OrganizeRules.from_config()
        if overrides:
            allowed_overrides = {
                key: bool(overrides[key])
                for key in ("region_split", "year_split")
                if key in overrides
            }
            if allowed_overrides:
                rules = replace(rules, **allowed_overrides)
        rules = enforce_fixed_organize_rules(rules)
        effective_rules_snapshot = self._serialize_rules_snapshot(rules)

        cleanup_candidates = discover_cleanup_candidates(inspection.root, inspection.selected_path)
        cleanup_paths = {item.snapshot.path for item in cleanup_candidates}
        videos = [item for item in current if item.role == "video" and item.path not in cleanup_paths]
        subtitles = [_LocalFile(item.relative_path, item.path.name, item) for item in current if item.role == "subtitle"]
        video_files = [_LocalFile(item.relative_path, item.path.name, item) for item in videos]
        # 本地预览共享一个有界预算：缓存命中免费，慢文件不会把整次预览拖成 N×30 秒。
        probe_budget = ProbeBudget(attempts=24, max_seconds=20)
        subtitle_result = plan_subtitle_companions(video_files, subtitles)
        # 一个视频可有多个语言字幕，使用 multimap。
        subtitle_groups: dict[str, list] = {}
        for item in subtitle_result.plans:
            subtitle_groups.setdefault(item.video_file_id, []).append(item)

        effective_media_type = str(media_type or "").strip().lower()
        if effective_media_type not in {"movie", "tv"}:
            effective_media_type = source.media_type if source.media_type in {"movie", "tv"} else ""
        if (season_override is not None or episode_override is not None) and effective_media_type == "movie":
            raise LocalMediaServiceError("电影整理不能指定季数或集数")

        plans: list[LocalMovePlan] = []
        matches: list[dict[str, Any]] = []
        seen_targets: set[Path] = set()
        for video in videos:
            match = self._match_video(
                video, tmdb_id=tmdb_id, media_type=effective_media_type, rules=rules,
            )
            match_identity = self.organizer._match_external_id(match)
            if (season_override is not None or episode_override is not None) and match.media_type != "tv":
                raise LocalMediaServiceError("只有剧集整理可以指定季数或集数")
            automatic_policy = automatic_match_policy(rules.automatic_match_preset)
            automatic_requires_confirmation = bool(
                not tmdb_id
                and automatic
                and automatic_match_requires_confirmation(
                    match,
                    threshold=automatic_policy.threshold,
                )
            )
            if (
                not match_identity
                or (match.need_confirm and not (
                    automatic and not tmdb_id and not automatic_requires_confirmation
                ))
                or automatic_requires_confirmation
            ):
                candidates = self._confirmation_candidates(match)
                return {
                    "inspection_id": inspection_id,
                    "status": "requires_manual",
                    "reason": (
                        automatic_match_confirmation_message(automatic_policy.name)
                        if automatic_requires_confirmation
                        else (match.error or "媒体识别结果需要人工确认")
                    ),
                    "candidate": candidates[0] if candidates else {
                        "tmdb_id": match.tmdb_id, "title": match.title, "year": match.year,
                        "media_type": match.media_type, "confidence": match.confidence,
                        "provider": self.organizer._match_provider(match),
                        "external_id": match_identity,
                    },
                    "candidates": candidates,
                    "files": [{"name": item.path.name} for item in videos],
                    "snapshot_digest": inspection.digest,
                    "plans": [], "cloud_write": False,
                    "rules_snapshot": effective_rules_snapshot,
                }
            release_parse = self.scraper.parse_media(
                video.path.name, self._relative_parent(video),
                match if not tmdb_id else None,
            )
            # 人工指定 TMDB 只固定媒体身份，不应在确认后再被通用季集偏移改写。
            # 自动识别消费统一有效位置；人工身份锁定继续使用原始文件位置。
            parsed: dict[str, object] = {
                "title": release_parse.title,
                "year": release_parse.year,
                "type": release_parse.media_type,
                "tmdb_id": release_parse.tmdb_id,
                "season": (
                    release_parse.effective_season
                    if not tmdb_id else release_parse.source_season
                ),
                "episode": (
                    release_parse.effective_episode
                    if not tmdb_id else release_parse.source_episode
                ),
            }
            if tmdb_id and match.season_override is not None:
                parsed["season"] = match.season_override
            if match.media_type == "tv":
                if season_override is not None:
                    parsed["season"] = season_override
                if episode_override is not None:
                    parsed["episode"] = episode_override
            if match.media_type == "tv" and parsed.get("episode") is not None and parsed.get("season") is None:
                parsed["season"] = 1
            if match.media_type == "tv" and parsed.get("episode") is None:
                candidates = self._confirmation_candidates(match)
                return {
                    "inspection_id": inspection_id, "status": "requires_manual",
                    "reason": f"剧集文件缺少集数，不能自动归档: {video.path.name}",
                    "candidate": candidates[0] if candidates else {
                        "tmdb_id": match.tmdb_id, "title": match.title, "year": match.year,
                        "media_type": match.media_type, "confidence": match.confidence,
                        "provider": self.organizer._match_provider(match),
                        "external_id": match_identity,
                    },
                    "candidates": candidates,
                    "files": [{"name": item.path.name} for item in videos],
                    "snapshot_digest": inspection.digest,
                    "plans": [], "cloud_write": False,
                    "rules_snapshot": effective_rules_snapshot,
                }
            proxy = GuangYaFile(video.relative_path, video.path.name, False, video.size)
            media_profile = probe_local_media_profile(
                video.path,
                size=video.size,
                mtime_ns=video.mtime_ns,
                device=video.device,
                inode=video.inode,
                timeout=rules.media_probe_timeout,
                budget=probe_budget,
            )
            target_name = self.organizer.build_new_name(
                match,
                proxy,
                parsed,
                rules,
                media_info_override=media_profile.render() if media_profile else "",
                media_variant_override=media_profile,
            )
            main, region, year = self.organizer.classify(match, rules)
            category = _CATEGORY_KEYS.get(main, "default")
            target_config = self._target_for_category(targets, category)
            if target_config is None:
                raise LocalMediaServiceError(f"未配置 {main} 或默认归档目标")
            parts: list[str] = []
            if target_config.category == "default":
                parts.append(main)
            if rules.region_split:
                parts.append(region)
            if rules.year_split and year:
                parts.append(year)
            parts.extend(self.organizer.build_media_path_parts(match, parsed, rules))
            destination_dir = Path(target_config.path).expanduser().resolve(strict=False).joinpath(*parts)
            target = destination_dir / target_name
            if target in seen_targets:
                raise LocalMediaServiceError(f"本地整理计划包含重复目标: {target.name}")
            video_action = "move"
            video_note = ""
            video_target_identity: tuple[int, int, int, int] | None = None
            try:
                video_target_identity = LocalFilesystemAdapter.regular_file_identity(target)
            except LocalContentChanged:
                pass
            if video_target_identity is not None:
                if not automatic:
                    video_action = "replace"
                    video_note = "手动整理将安全覆盖并替换媒体库中的现有文件"
                else:
                    existing = GuangYaFile(str(target), target.name, False, video_target_identity[0])
                    incoming = GuangYaFile(video.relative_path, video.path.name, False, video.size)
                    if self.organizer.should_replace(
                        existing, incoming, target_name, rules,
                        existing_evidence=target.name,
                        incoming_evidence=video.path.name,
                    ):
                        video_action = "replace"
                        video_note = "按统一冲突策略替换媒体库中的现有文件"
                    else:
                        video_action = "skip"
                        video_note = "按统一冲突策略保留媒体库现有文件"
            seen_targets.add(target)
            group_id = f"{match.media_type}:{self.organizer._match_identity_key(match)}:{video.relative_path}"
            plans.append(LocalMovePlan(
                video, target, "video", group_id, action=video_action, note=video_note,
                provider=target_config.provider, library_id=target_config.library_id,
                library_name=target_config.library_name,
                expected_target_identity=(
                    video_target_identity if video_action == "replace" else None
                ),
            ))
            for subtitle_plan in subtitle_groups.get(video.relative_path, []):
                subtitle_snapshot = subtitle_plan.file.snapshot
                subtitle_target = destination_dir / subtitle_plan.target_name(target_name)
                if subtitle_target in seen_targets:
                    raise LocalMediaServiceError(f"本地整理计划包含重复字幕目标: {subtitle_target.name}")
                subtitle_action = "skip" if video_action == "skip" else "move"
                subtitle_note = "对应视频保留现有版本，字幕不移动" if video_action == "skip" else ""
                subtitle_target_identity: tuple[int, int, int, int] | None = None
                try:
                    subtitle_target_identity = LocalFilesystemAdapter.regular_file_identity(subtitle_target)
                except LocalContentChanged:
                    pass
                if subtitle_target_identity is not None and video_action != "skip":
                    subtitle_action = "replace"
                    subtitle_note = "随视频替换现有字幕"
                seen_targets.add(subtitle_target)
                plans.append(LocalMovePlan(
                    subtitle_snapshot, subtitle_target, "subtitle", group_id,
                    action=subtitle_action, note=subtitle_note,
                    provider=target_config.provider, library_id=target_config.library_id,
                    library_name=target_config.library_name,
                    expected_target_identity=(
                        subtitle_target_identity if subtitle_action == "replace" else None
                    ),
                ))
            matches.append({
                "tmdb_id": match.tmdb_id, "title": match.title, "year": match.year,
                "media_type": match.media_type, "confidence": match.confidence,
                "provider": self.organizer._match_provider(match),
                "external_id": match_identity,
                "source_name": video.path.name, "target_name": target_name,
                "season": parsed.get("season"), "episode": parsed.get("episode"),
                "category": main, "target_root": target_config.path,
            })
        return {
            "inspection_id": inspection_id,
            "status": "planned",
            "digest": inspection.digest,
            "cloud_write": False,
            "rules_snapshot": effective_rules_snapshot,
            "position_overrides": {
                "season": season_override, "episode": episode_override,
            },
            "matches": matches,
            "plans": [
                {
                    "source_path": item.source.relative_path,
                    "target_path": str(item.target),
                    "target_name": item.target.name,
                    "role": item.role,
                    "media_group": item.media_group,
                    "action": item.action,
                    "note": item.note,
                }
                for item in plans
            ],
            "subtitle_skipped": [
                {"name": item.file.name, "reason": item.reason, "reason_code": item.reason_code}
                for item in subtitle_result.skipped
            ],
            "cleanup": [
                {"name": item.snapshot.path.name, "reason": item.reason, "reason_code": item.reason_code}
                for item in cleanup_candidates
            ],
            "_move_plans": plans,
            "_cleanup_candidates": cleanup_candidates,
        }


    @staticmethod
    def _refresh_paths(paths: set[str]) -> list[str]:
        """兼容未绑定目标：逐服务器、逐路径隔离失败，避免一处异常中断整批刷新。"""
        warnings: list[str] = []
        providers: list[tuple[str, Any]] = []
        if get_bool("JELLYFIN_ENABLED"):
            try:
                from app.clients.jellyfin import JellyfinClient
                providers.append((
                    "Jellyfin",
                    JellyfinClient(get("JELLYFIN_URL"), get("JELLYFIN_API_KEY")),
                ))
            except Exception as exc:
                warnings.append(f"Jellyfin 客户端初始化失败: {exc}")
        if get_bool("EMBY_ENABLED"):
            try:
                from app.clients.emby import EmbyClient
                providers.append((
                    "Emby",
                    EmbyClient(get("EMBY_URL"), get("EMBY_TOKEN")),
                ))
            except Exception as exc:
                warnings.append(f"Emby 客户端初始化失败: {exc}")

        for label, client in providers:
            for path in sorted(paths):
                try:
                    refresher = getattr(client, "refresh_for_path", None)
                    ok = refresher(path) if callable(refresher) else client.refresh_all()
                    if not ok:
                        warnings.append(f"{label} 刷新失败: {path}")
                except Exception as exc:
                    warnings.append(f"{label} 刷新失败 {path}: {exc}")
        return warnings

    @staticmethod
    def _refresh_plans(plans: list[LocalMovePlan]) -> list[str]:
        """优先按稳定媒体库 ID 精准刷新；旧绑定按唯一名称安全兼容。"""
        warnings: list[str] = []
        bindings = {
            (
                item.provider.strip().lower(),
                item.library_id.strip(),
                item.library_name.strip(),
            )
            for item in plans
            if item.provider.strip() and (item.library_id.strip() or item.library_name.strip())
        }
        unbound_paths = {
            str(item.target.parent) for item in plans
            if not item.provider.strip()
        }
        if unbound_paths:
            warnings.extend(LocalMediaService._refresh_paths(unbound_paths))
        if not bindings:
            return warnings

        from app.modules.media_server_profiles import list_configured_profiles
        profiles = {
            item.server_type: item for item in list_configured_profiles()
            if item.enabled and item.configured
        }
        clients: dict[str, Any] = {}
        folders_by_provider: dict[str, list[dict[str, Any]]] = {}
        for provider, library_id, library_name in sorted(bindings):
            profile = profiles.get(provider)
            label = library_name or library_id
            if profile is None:
                warnings.append(f"{provider} 未启用或未配置，未刷新媒体库 {label}")
                continue
            try:
                client = clients.get(provider)
                if client is None:
                    if provider == "jellyfin":
                        from app.clients.jellyfin import JellyfinClient
                        client = JellyfinClient(profile.url, profile.credential)
                    elif provider == "emby":
                        from app.clients.emby import EmbyClient
                        client = EmbyClient(profile.url, profile.credential)
                    else:
                        warnings.append(f"不支持的媒体服务器: {provider}")
                        continue
                    clients[provider] = client

                resolved_id = library_id
                if not resolved_id:
                    folders = folders_by_provider.get(provider)
                    if folders is None:
                        folders = client.list_virtual_folders()
                        folders_by_provider[provider] = folders
                    matches = [
                        item for item in folders
                        if item["name"].casefold() == library_name.casefold()
                    ]
                    if len(matches) != 1:
                        reason = "不存在" if not matches else "存在同名媒体库"
                        warnings.append(f"{profile.label} {reason}，请重新绑定: {library_name}")
                        continue
                    resolved_id = matches[0]["id"]

                if not client.refresh_library(resolved_id):
                    warnings.append(f"{profile.label} 刷新失败: {label}")
            except Exception as exc:
                warnings.append(f"{profile.label} 刷新失败 {label}: {exc}")
        return warnings

    def execute_task(self, owner: str, task_id: int, *, qb_client=None) -> dict[str, Any]:
        task = db.get_local_media_task(task_id, owner=owner)
        if task is None:
            raise LocalMediaServiceError("本地媒体任务不存在")
        source = db.get_local_media_source(task.source_id, owner=owner)
        if source is None:
            raise LocalMediaServiceError("本地媒体来源不存在")
        rules = self._restore_rules_snapshot(task.rules_snapshot) if task.rules_snapshot else OrganizeRules.from_config()
        paused = False
        try:
            if task.qb_hash and qb_client is None:
                raise LocalMediaServiceError("qB 任务缺少可用客户端，已拒绝移动源文件")
            inspection = self.inspect_source(owner, task.source_id, task.content_path)
            preview = self.preview(
                owner, inspection["inspection_id"], task.tmdb_id, task.media_type,
                rules_snapshot=task.rules_snapshot,
                automatic=task.trigger in {"scan", "qb_completed"},
                season_override=task.season_override, episode_override=task.episode_override,
            )
            if preview.get("status") != "planned":
                db.update_local_media_task(
                    task_id, owner=owner, status="requires_manual",
                    error=str(preview.get("reason") or "TMDB 结果需要人工确认"),
                )
                return {"status": "requires_manual", "task_id": task_id, "preview": preview}
            for plan in preview["_move_plans"]:
                db.add_local_media_task_item(
                    task_id, str(plan.source.path), str(plan.target), role=plan.role,
                    media_group=plan.media_group, action=plan.action, size=plan.source.size,
                    mtime_ns=plan.source.mtime_ns, device=plan.source.device, inode=plan.source.inode,
                    owner=owner,
                )
            db.update_local_media_task(task_id, owner=owner, status="planned", error="")
            if source.mode == "preview_only":
                warning = "仅预览模式：未移动文件"
                db.update_local_media_task(
                    task_id, owner=owner, status="completed", warning=warning,
                    completed_at=db.now(), error="",
                )
                return {
                    "status": "completed", "task_id": task_id, "preview": preview,
                    "moved": [], "deleted_junk": [], "retained_junk": [],
                    "qb_cleanup_pending": False, "warnings": [warning],
                    "media_refresh_status": "skipped", "media": preview.get("matches", []),
                }
            if task.qb_hash and qb_client is not None:
                qb_client.pause_torrents(task.qb_hash)
                paused = True
            db.update_local_media_task(task_id, owner=owner, status="moving")
            executable_plans = [item for item in preview["_move_plans"] if item.action != "skip"]
            skipped_plans = [item for item in preview["_move_plans"] if item.action == "skip"]
            if executable_plans:
                result = LocalMoveTransaction(
                    [Path(source.local_root)],
                    [Path(item.path) for item in db.list_local_library_targets(task.source_id, owner=owner)],
                    task_id=task_id, owner=owner, operation_token=task.operation_token,
                ).execute(executable_plans)
            else:
                result = MoveTransactionResult(status="completed")
            db.update_local_media_task(task_id, owner=owner, status="verifying")
            warnings = list(result.warnings)
            if skipped_plans:
                warnings.append(f"按冲突策略保留现有文件，跳过 {len(skipped_plans)} 项")
            cleanup_allowed = not task.qb_hash
            qb_cleanup_pending = False
            if task.qb_hash and qb_client is not None:
                try:
                    qb_client.delete_torrents(task.qb_hash, delete_files=False)
                    cleanup_allowed = True
                except Exception:
                    qb_cleanup_pending = True
                    warnings.append(
                        "qB 任务移除失败：媒体已安全入库，任务保持暂停；"
                        "请在 qB 手动删除任务，来源垃圾文件已保留"
                    )
            cleanup_result = None
            cleanup_candidates = preview.get("_cleanup_candidates") or []
            if cleanup_allowed and (cleanup_candidates or rules.clean_empty):
                cleanup_result = delete_cleanup_items(
                    cleanup_candidates,
                    allowed_root=Path(source.local_root),
                    selected_path=Path(task.content_path),
                    remove_empty_dirs=bool(rules.clean_empty),
                )
                warnings.extend(cleanup_result.warnings)
            db.update_local_media_task(task_id, owner=owner, status="refreshing")
            media_refresh_status = "skipped"
            if rules.emby_refresh:
                refresh_warnings = self._refresh_plans(executable_plans)
                warnings.extend(refresh_warnings)
                media_refresh_status = "failed" if refresh_warnings else "completed"
            db.update_local_media_task(
                task_id, owner=owner, status="completed", warning="；".join(warnings),
                completed_at=db.now(), error="",
            )
            return {
                "status": "completed", "task_id": task_id,
                "moved": [str(item.target) for item in result.moved],
                "deleted_junk": list(cleanup_result.deleted) if cleanup_result else [],
                "retained_junk": list(cleanup_result.retained) if cleanup_result else [],
                "qb_cleanup_pending": qb_cleanup_pending,
                "warnings": warnings,
                "media_refresh_status": media_refresh_status,
                "media": [
                    {
                        "tmdb_id": item.get("tmdb_id", ""),
                        "title": item.get("title", ""),
                        "year": item.get("year", ""),
                        "media_type": item.get("media_type", ""),
                        "category": item.get("category", ""),
                        "source_name": item.get("source_name", ""),
                        "target_name": item.get("target_name", ""),
                    }
                    for item in preview.get("matches", [])
                ],
            }
        except Exception as exc:
            if paused and task.qb_hash and qb_client is not None:
                try:
                    qb_client.resume_torrents(task.qb_hash)
                except Exception as resume_exc:
                    exc = LocalMediaServiceError(f"{exc}；qB 恢复失败: {resume_exc}")
            db.update_local_media_task(task_id, owner=owner, status="failed", error=str(exc))
            raise

    def create_manual_task(
        self, owner: str, inspection_id: str, *, tmdb_id: str = "", media_type: str = "",
        rules_snapshot: str = "", season_override: int | None = None,
        episode_override: int | None = None,
    ) -> int:
        inspection = self.inspections.get(owner, inspection_id)
        season_override, episode_override = self._normalize_position_overrides(
            inspection, season_override, episode_override,
        )
        normalized_type = str(media_type or "").strip().lower()
        if normalized_type and normalized_type not in {"movie", "tv"}:
            raise LocalMediaServiceError("媒体类型必须是 movie 或 tv")
        source = db.get_local_media_source(inspection.source_id, owner=owner)
        effective_type = normalized_type or (source.media_type if source else "")
        if (season_override is not None or episode_override is not None) and effective_type != "tv":
            raise LocalMediaServiceError("只有剧集整理可以指定季数或集数")
        normalized_snapshot = ""
        if rules_snapshot:
            normalized_snapshot = self._serialize_rules_snapshot(
                self._restore_rules_snapshot(rules_snapshot)
            )
        return db.prepare_manual_local_media_task(
            inspection.source_id, str(inspection.selected_path), owner=owner,
            tmdb_id=str(tmdb_id or "").strip(), media_type=normalized_type,
            rules_snapshot=normalized_snapshot, season_override=season_override,
            episode_override=episode_override,
        )

    def execute_preview(self, owner: str, inspection_id: str, preview: dict[str, Any]) -> MoveTransactionResult:
        inspection = self.inspections.get(owner, inspection_id)
        if preview.get("status") != "planned" or not preview.get("_move_plans"):
            raise LocalMediaServiceError("预览尚未达到可执行状态")
        targets = db.list_local_library_targets(inspection.source_id, owner=owner)
        transaction = LocalMoveTransaction(
            [inspection.root], [Path(item.path) for item in targets], owner=owner,
        )
        return transaction.execute(preview["_move_plans"])


_service: LocalMediaService | None = None
_service_lock = threading.Lock()


def get_local_media_service() -> LocalMediaService:
    global _service
    with _service_lock:
        if _service is None:
            _service = LocalMediaService()
        return _service
