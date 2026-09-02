"""本地媒体检查、识别预览和移动编排服务。"""
from __future__ import annotations

import json
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from functools import wraps
from pathlib import Path
from typing import Any

from app import database as db
from app.clients.base import close_media_server_client
from app.clients.guangya import GuangYaFile
from app.modules.directory_media import (
    DirectoryInspection,
    DirectoryMediaInspector,
    MediaSnapshot,
    _VideoIdentity,
)
from app.modules.directory_scrape import DirectoryScrapeService, FixedMatchScraper
from app.modules.directory_scrape_errors import DirectoryScrapeRequestError
from app.modules.episode_mapping import NUMBERING_MODES, normalize_numbering_mode
from app.modules.local_move_transaction import LocalMoveTransaction, MoveTransactionResult
from app.modules.local_media_candidates import move_candidate_to_trash
from app.modules.local_media_models import (
    canonical_local_media_content_path,
    local_media_paths_overlap,
)
from app.modules.process_lock import CrossProcessLock
from app.modules.qb_control import qb_control_write_lease
from app.modules.local_path_mapping import (
    assert_within,
    require_container_absolute_path,
    validate_source_target_roots,
)
from app.modules.local_storage import (
    LocalContentChanged,
    LocalFileSnapshot,
    LocalFilesystemAdapter,
    LocalStorageError,
    snapshot_digest,
)
from app.modules.local_media_cleanup import (
    cleanup_candidates_from_snapshots,
    delete_cleanup_items,
    probable_sample_video_paths,
)
from app.modules.local_media_recognition_summary import (
    build_recognition_summary,
    infer_recognition_summary as infer_local_recognition_summary,
    serialize_recognition_summary,
)
from app.modules.organize import (
    OrganizePlan,
    OrganizeRules,
    Organizer,
    enforce_fixed_organize_rules,
)
from app.modules.organize_scan import OrganizeScanResult, ScannedVideo
from app.modules.organize_postprocess import media_role
from app.modules.media_probe import ProbeBudget, probe_local_media_profile
from app.modules.media_server_path_mapping import (
    MediaServerPathMapping,
    configured_media_server_refresh_options,
)
from app.modules.scraper import MatchResult, TMDBScraper
from app.logger import get_logger
from app.modules.special_media import (
    is_special_media_name,
    is_special_path,
    special_parent_context,
)


logger = get_logger(__name__)

# 目标库存读取、冲突仲裁和文件提交必须是一个跨入口原子区间。Web、TG、
# 调度器以及多 ASGI 进程共同使用同一把锁；底层移动事务仍保留自己的
# fail-fast 锁，二者名称不同，不会发生递归锁死。
_LOCAL_MEDIA_PIPELINE_WRITE_LOCK = CrossProcessLock("local-media-pipeline-write")


@contextmanager
def _local_media_write_lease():
    if not _LOCAL_MEDIA_PIPELINE_WRITE_LOCK.acquire(blocking=True):  # pragma: no cover
        raise LocalMediaServiceError("本地媒体写入队列暂不可用，请稍后重试")
    try:
        yield
    finally:
        _LOCAL_MEDIA_PIPELINE_WRITE_LOCK.release()


_SERVER_ONLY_RULE_FIELDS = {
    "nsfw_enabled",
    "nsfw_source_ids",
    "nsfw_exclusive",
    "nsfw_metatube_endpoint",
    "nsfw_metatube_token",
    "nsfw_category_name",
    "nsfw_strip_domains",
    "nsfw_timeout_seconds",
}


class LocalMediaServiceError(RuntimeError):
    """可安全显示的本地媒体业务错误。"""


class LocalMediaPostMoveError(LocalMediaServiceError):
    """不可逆整理步骤已提交或回滚不完整；上层必须保持人工核验语义。"""

    requires_manual = True


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
    local_target_root: str = ""
    server_path: str = ""
    expected_target_identity: tuple[int, int, int, int] | None = None
    retire_target: Path | None = None
    expected_retire_identity: tuple[int, int, int, int] | None = None


@dataclass
class _Inspection:
    owner: str
    source_id: int
    root: Path
    selected_path: Path
    snapshots: list[LocalFileSnapshot]
    digest: str
    created_at: float
    media_type: str = "movie"


class _InspectionStore:
    def __init__(
        self,
        ttl_seconds: int = 1800,
        *,
        max_records: int = 64,
        max_snapshot_entries: int = 40_000,
    ) -> None:
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_records = max(1, int(max_records))
        self.max_snapshot_entries = max(1, int(max_snapshot_entries))
        self._records: OrderedDict[str, _Inspection] = OrderedDict()
        self._scope_ids: dict[tuple[str, int, str], str] = {}
        self._snapshot_entries = 0
        self._lock = threading.RLock()

    @staticmethod
    def _scope_key(record: _Inspection) -> tuple[str, int, str]:
        return (
            str(record.owner),
            int(record.source_id),
            str(record.selected_path),
        )

    def _remove_locked(self, inspection_id: str) -> _Inspection | None:
        record = self._records.pop(str(inspection_id), None)
        if record is None:
            return None
        self._snapshot_entries = max(
            0, self._snapshot_entries - len(record.snapshots)
        )
        scope_key = self._scope_key(record)
        if self._scope_ids.get(scope_key) == str(inspection_id):
            self._scope_ids.pop(scope_key, None)
        return record

    def put(self, record: _Inspection) -> str:
        snapshot_count = len(record.snapshots)
        if snapshot_count > self.max_snapshot_entries:
            raise LocalMediaServiceError("检查结果过大，请缩小目录范围后重试")
        inspection_id = uuid.uuid4().hex
        with self._lock:
            self._prune_locked()
            previous_id = self._scope_ids.get(self._scope_key(record))
            if previous_id:
                self._remove_locked(previous_id)
            self._records[inspection_id] = record
            self._scope_ids[self._scope_key(record)] = inspection_id
            self._snapshot_entries += snapshot_count
            while (
                len(self._records) > self.max_records
                or self._snapshot_entries > self.max_snapshot_entries
            ):
                oldest_id = next(iter(self._records))
                self._remove_locked(oldest_id)
        return inspection_id

    def get(self, owner: str, inspection_id: str) -> _Inspection:
        with self._lock:
            self._prune_locked()
            normalized_id = str(inspection_id)
            record = self._records.get(normalized_id)
            if record is None or record.owner != str(owner):
                raise LocalMediaServiceError("检查记录不存在或已过期")
            self._records.move_to_end(normalized_id)
            return record

    def discard(self, owner: str, inspection_id: str) -> bool:
        with self._lock:
            self._prune_locked()
            normalized_id = str(inspection_id)
            record = self._records.get(normalized_id)
            if record is None or record.owner != str(owner):
                return False
            self._remove_locked(normalized_id)
            return True

    def consume(self, owner: str, inspection_id: str) -> _Inspection:
        with self._lock:
            self._prune_locked()
            normalized_id = str(inspection_id)
            record = self._records.get(normalized_id)
            if record is None or record.owner != str(owner):
                raise LocalMediaServiceError("检查记录不存在或已过期")
            consumed = self._remove_locked(normalized_id)
            assert consumed is not None
            return consumed

    def _prune_locked(self) -> None:
        deadline = time.time() - self.ttl_seconds
        expired = [
            key for key, value in self._records.items()
            if value.created_at < deadline
        ]
        for key in expired:
            self._remove_locked(key)


_CATEGORY_KEYS = {
    "电影": "movie",
    "剧集": "tv",
    "动漫": "anime",
    "纪录片": "documentary",
    "综艺": "variety",
    "演唱会": "concert",
    "儿童节目": "kids",
}


def _local_media_operation(method):
    """把公开业务入口纳入服务生命周期，嵌套调用复用外层 lease。"""
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._lifecycle_operation():
            return method(self, *args, **kwargs)

    return guarded


class LocalMediaService:
    def __init__(
        self,
        scraper: TMDBScraper | None = None,
        *,
        inspection_store: _InspectionStore | None = None,
    ) -> None:
        self._owns_scraper = scraper is None
        self._closed = False
        self._closing = False
        self._scraper_closed = not self._owns_scraper
        self._close_call_lock = threading.Lock()
        self._lifecycle_condition = threading.Condition(threading.Lock())
        self._active_operations = 0
        self._operation_depth = threading.local()
        self._operation_lock = threading.RLock()
        self.scraper = scraper or TMDBScraper()
        self.organizer = Organizer(client=object(), scraper=self.scraper)
        self.inspections = inspection_store or _InspectionStore()

    def parallel_planning_safe(self) -> bool:
        """仅默认 TMDB Scraper 声明可共享缓存并发规划。"""
        closed, closing, _active = self._lifecycle_state()
        return not closed and not closing and type(self.scraper) is TMDBScraper

    def create_planning_worker(self) -> "LocalMediaService":
        """创建拥有独立 Organizer/检查仓的只读规划服务。"""
        if not self.parallel_planning_safe():
            raise LocalMediaServiceError("当前本地媒体识别器不支持并行规划")
        worker = LocalMediaService(scraper=self.scraper)
        # 与光鸭组级并行保持同一契约：只共享强制详情刷新去重状态，
        # 其余 Organizer 任务缓存、ProbeBudget 和目标库存均由 Worker 独享。
        worker.organizer._forced_detail_refreshes = (
            self.organizer._forced_detail_refreshes
        )
        worker.organizer._forced_detail_refresh_lock = (
            self.organizer._forced_detail_refresh_lock
        )
        return worker

    @contextmanager
    def _lifecycle_operation(self):
        depth = int(getattr(self._operation_depth, "value", 0) or 0)
        if depth:
            self._operation_depth.value = depth + 1
            try:
                yield
            finally:
                self._operation_depth.value = depth
            return

        with self._lifecycle_condition:
            if self._closed or self._closing:
                raise LocalMediaServiceError("本地媒体服务正在关闭，请稍后重试")
            self._active_operations += 1
        self._operation_depth.value = 1
        try:
            yield
        finally:
            try:
                del self._operation_depth.value
            except AttributeError:
                pass
            with self._lifecycle_condition:
                self._active_operations -= 1
                finish_close = (
                    self._active_operations == 0
                    and self._closing
                    and not self._closed
                )
                self._lifecycle_condition.notify_all()
            if finish_close:
                self.close()

    def _lifecycle_state(self) -> tuple[bool, bool, int]:
        with self._lifecycle_condition:
            return self._closed, self._closing, self._active_operations

    def close(self) -> bool:
        """幂等释放服务资源；在途业务完成前不抢先关闭底层客户端。"""
        with self._close_call_lock:
            with self._lifecycle_condition:
                if self._closed:
                    return True
                self._closing = True
                if self._active_operations:
                    return False

            with self._operation_lock:
                try:
                    organizer_closed = self.organizer.close()
                except Exception as exc:
                    logger.warning(
                        "关闭本地媒体 Organizer 失败 type=%s", type(exc).__name__
                    )
                    organizer_closed = False
                if organizer_closed is None:
                    organizer_closed = True
                if self._owns_scraper and not self._scraper_closed:
                    close = getattr(self.scraper, "close", None)
                    if callable(close):
                        try:
                            closed = close()
                        except Exception as exc:
                            logger.warning(
                                "关闭本地媒体 TMDB Scraper 失败 type=%s",
                                type(exc).__name__,
                            )
                            closed = False
                        self._scraper_closed = closed is not False
                    else:
                        self._scraper_closed = True
                resources_closed = bool(organizer_closed and self._scraper_closed)

            if resources_closed:
                with self._lifecycle_condition:
                    self._closed = True
                    self._closing = False
                    self._lifecycle_condition.notify_all()
            return resources_closed

    @_local_media_operation
    def inspect_source(self, owner: str, source_id: int, path: Path | str) -> dict[str, Any]:
        source = db.get_local_media_source(source_id, owner=owner)
        if source is None:
            raise LocalMediaServiceError("本地媒体来源不存在")
        root = require_container_absolute_path(source.local_root, label="来源目录")
        assert_within(root, root)
        selected = assert_within(
            require_container_absolute_path(path, label="目录路径"), root,
        )
        adapter = LocalFilesystemAdapter(root)
        snapshots = adapter.scan(selected, include_non_media=True)
        visible_snapshots = [
            item for item in snapshots
            if item.size > 0
            and item.role != "other"
            and not adapter.is_temporary(item.path)
        ]
        videos = [item for item in visible_snapshots if item.role == "video"]
        if not videos:
            raise LocalMediaServiceError("选择路径中没有可整理的视频")
        primary_video = videos[0] if len(videos) == 1 else None
        suggested_query = ""
        parsed_season: int | None = None
        parsed_episode: int | None = None
        detected_media_type = (
            source.media_type if source.media_type in {"movie", "tv"} else ""
        )
        if primary_video is not None:
            try:
                parsed = self.scraper.parse_media(
                    primary_video.path.name, self._relative_parent(primary_video),
                )
            except Exception:
                parsed = None
            suggested_query = str(getattr(parsed, "title", "") or "").strip()
            parsed_season = getattr(parsed, "effective_season", None)
            parsed_episode = getattr(parsed, "effective_episode", None)
            if not detected_media_type:
                parsed_type = str(
                    getattr(parsed, "media_type", "") or ""
                ).strip().lower()
                detected_media_type = (
                    "tv"
                    if parsed_season is not None or parsed_episode is not None
                    else parsed_type if parsed_type in {"movie", "tv"} else ""
                )
            if not suggested_query:
                clean_title = getattr(self.scraper, "clean_title", None)
                if callable(clean_title):
                    suggested_query = str(clean_title(primary_video.path.name) or "").strip()
            if not suggested_query:
                suggested_query = primary_video.path.stem
        else:
            try:
                parsed = self.scraper.parse_media(selected.name)
            except Exception:
                parsed = None
            if parsed is not None:
                suggested_query = str(getattr(parsed, "title", "") or "").strip()
                if not detected_media_type:
                    parsed_type = str(
                        getattr(parsed, "media_type", "") or ""
                    ).strip().lower()
                    parsed_season = getattr(parsed, "effective_season", None)
                    parsed_episode = getattr(parsed, "effective_episode", None)
                    # 多视频目录名只有出现明确剧集证据时才可锁定类型；
                    # 普通 staging/downloads 目录不能仅因解析器回退就判成电影。
                    if (
                        parsed_type == "tv"
                        or parsed_season is not None
                        or parsed_episode is not None
                    ):
                        detected_media_type = "tv"
            clean_title = getattr(self.scraper, "clean_title", None)
            if not suggested_query and callable(clean_title):
                suggested_query = str(clean_title(selected.name) or "").strip()
            if not suggested_query:
                suggested_query = selected.name
        if detected_media_type not in {"movie", "tv"}:
            detected_media_type = "movie"
            for item in videos[:8]:
                source_season = source_episode = None
                try:
                    source_season, source_episode = self.scraper.parse_source_position(
                        item.path.name, self._relative_parent(item),
                    )
                except Exception:
                    try:
                        parsed_item = self.scraper.parse_media(
                            item.path.name, self._relative_parent(item),
                        )
                    except Exception:
                        parsed_item = None
                    source_season = getattr(parsed_item, "effective_season", None)
                    source_episode = getattr(parsed_item, "effective_episode", None)
                    if str(
                        getattr(parsed_item, "media_type", "") or ""
                    ).strip().lower() == "tv":
                        detected_media_type = "tv"
                        break
                if source_season is not None or source_episode is not None:
                    detected_media_type = "tv"
                    break
        digest = snapshot_digest(snapshots)
        inspection_id = self.inspections.put(_Inspection(
            owner=str(owner), source_id=int(source_id), root=root, selected_path=selected,
            snapshots=snapshots, digest=digest, created_at=time.time(),
            media_type=detected_media_type,
        ))
        return {
            "inspection_id": inspection_id,
            "source_id": int(source_id),
            "selected_name": selected.name,
            "selected_kind": "file" if selected.is_file() else "directory",
            "file_count": len(visible_snapshots),
            "video_count": len(videos),
            "single_video": primary_video is not None,
            "primary_video_name": primary_video.path.name if primary_video else "",
            "suggested_query": suggested_query,
            "media_type": detected_media_type,
            "parsed_season": parsed_season,
            "parsed_episode": parsed_episode,
            "digest": digest,
            "cloud_write": False,
            "files": [
                {"name": item.path.name, "relative_path": item.relative_path, "role": item.role, "size": item.size}
                for item in visible_snapshots
            ],
        }

    @_local_media_operation
    def inspect_task(self, owner: str, task_id: int) -> dict[str, Any]:
        task = db.get_local_media_task(task_id, owner=owner)
        if task is None:
            raise LocalMediaServiceError("本地媒体任务不存在")
        if task.status != "requires_manual":
            raise LocalMediaServiceError("仅待确认任务可以进入人工复核")
        result = self.inspect_source(owner, task.source_id, task.content_path)
        if str(getattr(task, "title", "") or "").strip():
            result["suggested_query"] = str(task.title).strip()
        result.update({
            "task_error": str(getattr(task, "error", "") or ""),
            "task_title": str(getattr(task, "title", "") or ""),
            "task_year": str(getattr(task, "year", "") or ""),
            "task_tmdb_id": str(getattr(task, "tmdb_id", "") or ""),
            "task_media_type": str(getattr(task, "media_type", "") or ""),
            "task_numbering_mode": str(
                getattr(task, "numbering_mode", "auto") or "auto"
            ),
        })
        return result

    @_local_media_operation
    def search(
        self,
        query: str,
        year: str = "",
        media_type: str = "movie",
        *,
        owner: str = "admin",
        inspection_id: str = "",
    ) -> list[dict[str, Any]]:
        if inspection_id:
            inspection = self.inspections.get(owner, inspection_id)
            source = db.get_local_media_source(inspection.source_id, owner=owner)
            if source is not None and source.media_type == "nsfw":
                raise LocalMediaServiceError(
                    "成人番号专用来源不使用 TMDB 搜索，请直接运行 MetaTube 自动识别"
                )
        effective_type = str(media_type or "").strip().lower()
        if effective_type == "auto":
            if inspection_id:
                effective_type = self.inspections.get(owner, inspection_id).media_type
            if effective_type not in {"movie", "tv"}:
                effective_type = "movie"
        if effective_type not in {"movie", "tv"}:
            raise LocalMediaServiceError("媒体类型必须是 auto、movie 或 tv")
        return [
            {
                "tmdb_id": item.tmdb_id, "title": item.title, "year": item.year,
                "media_type": item.media_type or effective_type, "score": item.score,
                "overview": item.overview, "poster_path": item.poster_path,
            }
            for item in self.scraper.search_candidates(query, year, effective_type)
        ]

    @_local_media_operation
    def infer_recognition_summary(self, item_rows) -> dict[str, Any]:
        """在服务 lease 内执行历史摘要推断，避免关闭时裸用 Scraper。"""
        return infer_local_recognition_summary(item_rows, scraper=self.scraper)

    @_local_media_operation
    def external_hints(
        self,
        owner: str,
        inspection_id: str,
        query: str,
        media_type: str = "auto",
    ) -> dict[str, Any]:
        inspection = self.inspections.get(owner, inspection_id)
        source = db.get_local_media_source(inspection.source_id, owner=owner)
        if source is not None and source.media_type == "nsfw":
            raise LocalMediaServiceError(
                "成人番号专用来源不使用豆瓣/BGM 或普通 TMDB 线索"
            )
        search_query = str(query or "").strip()
        if not search_query:
            raise LocalMediaServiceError("请输入外部资料搜索词")
        normalized_type = str(media_type or "auto").strip().lower()
        if normalized_type not in {"auto", "movie", "tv"}:
            raise LocalMediaServiceError("媒体类型只能是自动、电影或剧集")
        resolved_type = (
            inspection.media_type if normalized_type == "auto" else normalized_type
        )
        try:
            return DirectoryScrapeService.query_external_hints(
                search_query, resolved_type,
            )
        except Exception as exc:
            raise LocalMediaServiceError(str(exc)) from exc

    @staticmethod
    def _relative_parent(snapshot: LocalFileSnapshot) -> str:
        parent = Path(snapshot.relative_path).parent.as_posix()
        return "" if parent == "." else parent

    @staticmethod
    def _scope_relative_parent(
        inspection: _Inspection,
        snapshot: LocalFileSnapshot,
    ) -> str:
        if inspection.selected_path.is_file():
            return LocalMediaService._relative_parent(snapshot)
        try:
            parent = snapshot.path.parent.relative_to(inspection.selected_path).as_posix()
        except ValueError:
            return LocalMediaService._relative_parent(snapshot)
        return "" if parent == "." else parent

    @staticmethod
    def _local_etag(snapshot: LocalFileSnapshot) -> str:
        return ":".join(str(value) for value in (
            snapshot.mtime_ns, snapshot.device, snapshot.inode,
        ))

    def _build_local_scan_result(
        self,
        inspection: _Inspection,
        videos: list[LocalFileSnapshot],
        companions: list[LocalFileSnapshot],
    ) -> tuple[OrganizeScanResult, dict[str, LocalFileSnapshot]]:
        snapshots_by_id: dict[str, LocalFileSnapshot] = {}
        scanned_videos: list[ScannedVideo] = []
        companion_files: dict[str, list[GuangYaFile]] = {}
        video_files_by_path: dict[str, list[GuangYaFile]] = {}

        source_root_name = (
            "" if inspection.selected_path.is_file() else inspection.selected_path.name
        )
        for snapshot in videos:
            file_id = snapshot.relative_path
            snapshots_by_id[file_id] = snapshot
            relative_dir = self._scope_relative_parent(inspection, snapshot)
            proxy = GuangYaFile(
                file_id=file_id,
                name=snapshot.path.name,
                is_dir=False,
                size=snapshot.size,
                etag=self._local_etag(snapshot),
                parent_id=str(snapshot.path.parent.resolve(strict=False)),
            )
            special = bool(
                is_special_path(relative_dir)
                or is_special_media_name(snapshot.path.name)
            )
            scanned_videos.append(ScannedVideo(
                file=proxy,
                relative_dir=relative_dir,
                special=special,
                recognition_parent_path=(
                    special_parent_context(relative_dir, source_root_name)
                    if special else relative_dir
                ),
                source_group_id=str(inspection.selected_path),
                source_group_path=relative_dir or "__root__",
            ))
            video_files_by_path.setdefault(relative_dir, []).append(proxy)

        for snapshot in companions:
            file_id = snapshot.relative_path
            snapshots_by_id[file_id] = snapshot
            relative_dir = self._scope_relative_parent(inspection, snapshot)
            companion_files.setdefault(relative_dir, []).append(GuangYaFile(
                file_id=file_id,
                name=snapshot.path.name,
                is_dir=False,
                size=snapshot.size,
                etag=self._local_etag(snapshot),
                parent_id=str(snapshot.path.parent.resolve(strict=False)),
            ))

        return OrganizeScanResult(
            scanned_videos=scanned_videos,
            scanned_dirs=[],
            companion_files=companion_files,
            video_files_by_path=video_files_by_path,
            protected_sources=set(),
            # 目录整理与光鸭一致：选择目录名作为来源根标题证据，文件内部
            # 只保留相对路径；单文件整理则沿用来源根下的父目录上下文。
            source_root_name=source_root_name,
        ), snapshots_by_id

    def _build_local_directory_inspection(
        self,
        inspection: _Inspection,
        scan_result: OrganizeScanResult,
        *,
        media_type: str,
    ) -> DirectoryInspection:
        videos: list[MediaSnapshot] = []
        identities: list[_VideoIdentity] = []
        for item in scan_result.scanned_videos:
            parent_context = "/".join(
                value for value in (
                    scan_result.source_root_name, item.recognition_parent_path,
                ) if value
            )
            try:
                parsed = self.scraper.parse_media(item.file.name, parent_context)
            except Exception:
                parsed = None
            season = getattr(parsed, "source_season", None)
            episode = getattr(parsed, "source_episode", None)
            if season is None:
                season = getattr(parsed, "effective_season", None)
            if episode is None:
                episode = getattr(parsed, "effective_episode", None)
            try:
                source_season, source_episode = self.scraper.parse_source_position(
                    item.file.name, parent_context,
                )
            except Exception:
                source_season, source_episode = None, None
            if source_season is not None:
                season = source_season
            if source_episode is not None:
                episode = source_episode

            special_container = is_special_path(item.relative_dir)
            special = bool(item.special or special_container)
            if special:
                season = 0
            snapshot = MediaSnapshot(
                file_id=item.file.file_id,
                parent_id=item.file.parent_id,
                name=item.file.name,
                size=item.file.size,
                etag=item.file.etag,
                role="video",
                relative_dir=item.relative_dir,
                season=season,
                episode=episode,
            )
            videos.append(snapshot)
            parsed_type = str(
                getattr(parsed, "media_type", "") or ""
            ).strip().lower()
            identity_type = (
                "tv"
                if media_type == "tv"
                or parsed_type == "tv"
                or season is not None
                or episode is not None
                else "movie"
            )
            identities.append(_VideoIdentity(
                file=snapshot,
                special=special,
                special_container=special_container,
                media_type=identity_type,
                title=str(getattr(parsed, "title", "") or "").strip(),
                year=str(getattr(parsed, "year", "") or "").strip(),
                tmdb_id=str(getattr(parsed, "tmdb_id", "") or "").strip(),
            ))

        videos = DirectoryMediaInspector._assign_special_episodes(videos)
        snapshots_by_id = {item.file_id: item for item in videos}
        identities = [
            replace(item, file=snapshots_by_id[item.file.file_id])
            for item in identities
        ]
        try:
            selected_videos, pending_videos = (
                DirectoryMediaInspector._select_primary_media_group(identities)
            )
        except DirectoryScrapeRequestError as exc:
            raise LocalMediaServiceError(str(exc)) from exc

        companions: list[MediaSnapshot] = []
        for relative_dir, items in scan_result.companion_files.items():
            companions.extend(MediaSnapshot(
                file_id=item.file_id,
                parent_id=item.parent_id,
                name=item.name,
                size=item.size,
                etag=item.etag,
                role=media_role(item.name),
                relative_dir=relative_dir,
            ) for item in items)
        if pending_videos:
            companions = DirectoryMediaInspector._safe_companions_for_videos(
                selected_videos, companions,
            )

        return DirectoryInspection(
            directory_id=str(inspection.selected_path),
            directory_name=scan_result.source_root_name,
            media_type=media_type,
            suggested_query="",
            videos=tuple(selected_videos),
            companions=tuple(companions),
            counts={
                "video": len(selected_videos),
                "subtitle": sum(item.role == "subtitle" for item in companions),
                "metadata": sum(item.role != "subtitle" for item in companions),
            },
            mixed=bool(pending_videos),
            fingerprint=inspection.digest,
            pending_videos=tuple(pending_videos),
        )

    @staticmethod
    def _normalize_numbering_mode(value: str) -> str:
        mode = str(value or "auto").strip().lower()
        if mode not in NUMBERING_MODES:
            raise LocalMediaServiceError("剧集编号模式无效")
        return normalize_numbering_mode(mode)

    @staticmethod
    def _target_directory_for_plan(plan: OrganizePlan, targets):
        provider = str(getattr(plan.match, "provider", "") or "").strip().lower()
        # MetaTube 结果采用电影目录结构，但仍复用“媒体库”页面为当前来源
        # 选择的真实电影/默认归档映射。这里不能把成人分类名当成一套新的
        # 本地媒体库类型，否则会绕开用户已经配置好的媒体库下拉选择。
        category = (
            "movie"
            if provider in {"metatube", "clean_title"}
            else _CATEGORY_KEYS.get(plan.main_category, "default")
        )
        by_category = {item.category: item for item in targets}
        media_fallback = (
            "tv"
            if str(getattr(plan.match, "media_type", "") or "") == "tv"
            else "movie"
        )
        target_config = (
            by_category.get(category)
            or by_category.get(media_fallback)
            or by_category.get("default")
        )
        if target_config is None:
            raise LocalMediaServiceError(
                f"未配置 {plan.main_category or '当前媒体'} 或默认归档目标"
            )
        parts = [part for part in str(plan.target_path or "").split("/") if part]
        if (
            target_config.category != "default"
            and parts
            and parts[0] == plan.main_category
        ):
            parts = parts[1:]
        destination = Path(target_config.path).expanduser().resolve(
            strict=False
        ).joinpath(*parts)
        return target_config, destination

    @staticmethod
    def _local_target_inventory(
        destination: Path,
    ) -> tuple[str, list[GuangYaFile], dict[str, str]]:
        target_id = str(destination)
        if not destination.exists():
            return target_id, [], {}
        if not destination.is_dir() or destination.is_symlink():
            raise LocalMediaServiceError(
                f"本地归档目标不是安全目录: {destination.name}"
            )
        files: list[GuangYaFile] = []
        evidence: dict[str, str] = {}
        for path in destination.iterdir():
            try:
                size, mtime_ns, device, inode = (
                    LocalFilesystemAdapter.regular_file_identity(path)
                )
            except (LocalContentChanged, LocalStorageError, OSError):
                continue
            file_id = str(path.resolve(strict=False))
            item = GuangYaFile(
                file_id=file_id,
                name=path.name,
                is_dir=False,
                size=size,
                etag=f"{mtime_ns}:{device}:{inode}",
                parent_id=target_id,
            )
            files.append(item)
            evidence[file_id] = path.name
        return target_id, files, evidence

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
        if (
            episode_override is not None
            and not inspection.selected_path.is_file()
        ):
            raise LocalMediaServiceError("目录刮削只能统一指定归档季，不能覆盖全部集号")
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

    @_local_media_operation
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
        numbering_mode: str = "auto",
    ) -> dict[str, Any]:
        """串行生成预览，隔离共享 Organizer 的任务级缓存与探测预算。"""
        with self._operation_lock:
            return self._preview_locked(
                owner,
                inspection_id,
                tmdb_id,
                media_type,
                overrides,
                rules_snapshot,
                automatic,
                season_override,
                episode_override,
                numbering_mode,
            )

    def _preview_locked(
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
        numbering_mode: str = "auto",
    ) -> dict[str, Any]:
        inspection = self.inspections.get(owner, inspection_id)
        normalized_numbering_mode = self._normalize_numbering_mode(numbering_mode)
        season_override, episode_override = self._normalize_position_overrides(
            inspection, season_override, episode_override,
        )
        source = db.get_local_media_source(inspection.source_id, owner=owner)
        if source is None:
            raise LocalMediaServiceError("本地媒体来源已被删除")
        current = LocalFilesystemAdapter(inspection.root).scan(
            inspection.selected_path, include_non_media=True,
        )
        if snapshot_digest(current) != inspection.digest:
            raise LocalMediaServiceError("源文件在检查后发生变化，请重新检查")
        targets = db.list_local_library_targets(inspection.source_id, owner=owner)
        if not targets:
            raise LocalMediaServiceError("当前来源尚未配置归档目标")
        validate_source_target_roots(
            inspection.root, [Path(item.path) for item in targets],
        )

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
        rules = enforce_fixed_organize_rules(rules).for_local_source(source.media_type)
        nsfw_only = source.media_type == "nsfw"
        if nsfw_only and (
            not rules.nsfw_enabled
            or not str(rules.nsfw_metatube_endpoint or "").strip()
        ):
            raise LocalMediaServiceError(
                "该来源已设为成人番号专用，但 MetaTube 成人识别尚未启用或配置完整"
            )
        effective_rules_snapshot = self._serialize_rules_snapshot(rules)

        cleanup_candidates = cleanup_candidates_from_snapshots(current)
        cleanup_paths = {item.snapshot.path for item in cleanup_candidates}
        retained_sample_paths = probable_sample_video_paths(current)
        videos = [
            item for item in current
            if item.role == "video"
            and item.path not in cleanup_paths
            and item.path not in retained_sample_paths
        ]
        companions = [
            item for item in current
            if item.role in {"subtitle", "metadata", "image"}
            and item.path not in cleanup_paths
        ]
        if not videos:
            raise LocalMediaServiceError("选择路径中没有可整理的视频")

        requested_media_type = str(media_type or "").strip().lower()
        if nsfw_only:
            if str(tmdb_id or "").strip():
                raise LocalMediaServiceError(
                    "成人番号专用来源不能套用 TMDB 候选，只接受 MetaTube 精确番号结果"
                )
            if requested_media_type not in {"", "auto", "movie", "nsfw"}:
                raise LocalMediaServiceError("成人番号专用来源只能按电影结构归档")
            effective_media_type = "movie"
        else:
            if requested_media_type not in {"", "auto", "movie", "tv"}:
                raise LocalMediaServiceError("媒体类型必须是 auto、movie 或 tv")
            effective_media_type = requested_media_type
            if effective_media_type in {"", "auto"}:
                effective_media_type = (
                    inspection.media_type
                    if inspection.media_type in {"movie", "tv"}
                    else source.media_type
                )
            if effective_media_type not in {"movie", "tv"}:
                effective_media_type = "movie"
        if (
            season_override is not None or episode_override is not None
        ) and effective_media_type != "tv":
            raise LocalMediaServiceError("只有剧集整理可以指定季数或集数")
        if (
            not str(tmdb_id or "").strip()
            and (
                normalized_numbering_mode != "auto"
                or season_override is not None
                or episode_override is not None
            )
        ):
            raise LocalMediaServiceError("请先选择剧集候选，再指定编号方式或归档位置")

        scan_result, snapshots_by_id = self._build_local_scan_result(
            inspection, videos, companions,
        )
        planner_scraper = self.scraper
        position_overrides: dict[object, tuple[int | None, int | None]] = {}
        episode_mappings = {}
        fixed_match: MatchResult | None = None
        detail: dict[str, Any] = {}
        manual_pending_confirmations: list[dict[str, Any]] = []
        requested_tmdb_id = str(tmdb_id or "").strip()
        if requested_tmdb_id:
            fixed_match = self.scraper.match_from_tmdb(
                requested_tmdb_id, effective_media_type,
            )
            if not self.organizer._match_external_id(fixed_match) or fixed_match.need_confirm:
                raise LocalMediaServiceError("TMDB 详情不存在或无法确认")
            detail_loader = getattr(self.scraper, "get_detail_with_credits", None)
            if not callable(detail_loader):
                detail_loader = getattr(self.scraper, "get_detail", None)
            detail = (
                detail_loader(requested_tmdb_id, effective_media_type)
                if callable(detail_loader) else {}
            )
            if not detail:
                raise LocalMediaServiceError("TMDB 详情不存在或无法确认")
            directory_inspection = self._build_local_directory_inspection(
                inspection, scan_result, media_type=effective_media_type,
            )
            if directory_inspection.pending_videos:
                allowed_video_ids = {
                    item.file_id for item in directory_inspection.videos
                }
                allowed_companion_ids = {
                    item.file_id for item in directory_inspection.companions
                }
                scan_result = OrganizeScanResult(
                    scanned_videos=[
                        item for item in scan_result.scanned_videos
                        if item.file.file_id in allowed_video_ids
                    ],
                    scanned_dirs=list(scan_result.scanned_dirs),
                    companion_files={
                        relative_dir: [
                            item for item in items
                            if item.file_id in allowed_companion_ids
                        ]
                        for relative_dir, items in scan_result.companion_files.items()
                        if any(
                            item.file_id in allowed_companion_ids for item in items
                        )
                    },
                    video_files_by_path={
                        relative_dir: [
                            item for item in items
                            if item.file_id in allowed_video_ids
                        ]
                        for relative_dir, items in scan_result.video_files_by_path.items()
                        if any(item.file_id in allowed_video_ids for item in items)
                    },
                    protected_sources=set(scan_result.protected_sources),
                    source_root_name=scan_result.source_root_name,
                )
                manual_pending_confirmations = [
                    {
                        "source_name": item.file.name,
                        "reason": item.reason,
                        "candidate": None,
                        "candidates": [],
                    }
                    for item in directory_inspection.pending_videos
                ]
            season_detail_loader = getattr(
                self.scraper, "get_tv_season_detail", None,
            )
            position_overrides, episode_mappings = (
                DirectoryScrapeService._mapped_position_overrides(
                    directory_inspection,
                    detail,
                    normalized_numbering_mode,
                    season_override=season_override,
                    episode_override=episode_override,
                    season_detail_loader=(
                        season_detail_loader
                        if callable(season_detail_loader) else None
                    ),
                )
            )
            planner_scraper = FixedMatchScraper(
                self.scraper,
                fixed_match,
                detail,
                season_override=season_override,
                episode_override=episode_override,
                preserve_specials=not inspection.selected_path.is_file(),
                position_overrides=position_overrides,
            )

        planner = (
            self.organizer
            if planner_scraper is self.scraper
            else Organizer(client=object(), scraper=planner_scraper)
        )
        probe_budget = ProbeBudget(attempts=24, max_seconds=20)

        def load_media_profile(file: GuangYaFile, _plan: OrganizePlan):
            if not rules.media_info_enabled or not rules.media_probe_enabled:
                return None
            snapshot = snapshots_by_id.get(str(file.file_id))
            if snapshot is None or snapshot.role != "video":
                return None
            return probe_local_media_profile(
                snapshot.path,
                size=snapshot.size,
                mtime_ns=snapshot.mtime_ns,
                device=snapshot.device,
                inode=snapshot.inode,
                timeout=rules.media_probe_timeout,
                budget=probe_budget,
            )

        target_directory_cache: dict[str, tuple[object, Path]] = {}
        inventory_cache: dict[
            str, tuple[str, list[GuangYaFile], dict[str, str]]
        ] = {}

        def target_for_plan(plan: OrganizePlan):
            cached = target_directory_cache.get(str(plan.file_id))
            if cached is None:
                cached = self._target_directory_for_plan(plan, targets)
                target_directory_cache[str(plan.file_id)] = cached
            return cached

        def load_target_inventory(plan: OrganizePlan):
            _target_config, destination = target_for_plan(plan)
            key = str(destination)
            if key not in inventory_cache:
                inventory_cache[key] = self._local_target_inventory(destination)
            return inventory_cache[key]

        planning_result, planning_stats = planner.plan_scan_result(
            scan_result,
            rules,
            automatic=bool(automatic and not requested_tmdb_id),
            source_dir_id=str(inspection.selected_path),
            source_name="",
            media_type_hint=effective_media_type,
            media_profile_loader=load_media_profile,
            target_inventory_loader=load_target_inventory,
            # 光鸭历史日志不属于本地媒体库；本地仍复用同批身份绑定和
            # 目录标记校验，但历史来源必须由本地后端单独提供。
            identity_history_loader=lambda _plan: set(),
        )

        local_plans: list[LocalMovePlan] = []
        matches: list[dict[str, Any]] = []
        pending_confirmations: list[dict[str, Any]] = list(
            manual_pending_confirmations
        )
        seen_targets: set[Path] = set()

        for plan in planning_result.plans:
            match = plan.match or MatchResult()
            snapshot = snapshots_by_id.get(str(plan.file_id))
            if match.need_confirm or plan.action == "conflict":
                candidates = self._confirmation_candidates(match)
                pending_confirmations.append({
                    "source_name": plan.original_name,
                    "reason": plan.note or match.error or "媒体识别结果需要人工确认",
                    "candidate": candidates[0] if candidates else None,
                    "candidates": candidates,
                })
                continue
            if snapshot is None or not plan.new_name or not plan.target_path:
                continue
            target_config, destination_dir = target_for_plan(plan)
            target = destination_dir / plan.new_name
            action = "skip" if plan.action == "skip" else "move"
            if action != "skip":
                if target in seen_targets:
                    raise LocalMediaServiceError(
                        f"本地整理计划包含重复目标: {target.name}"
                    )
                seen_targets.add(target)

            note = plan.note or plan.conflict_note
            expected_target_identity = None
            retire_target = None
            expected_retire_identity = None
            if plan.action == "move" and plan.conflict_decision == "replace":
                action = "replace"
                existing_path = Path(plan.conflict_existing_id)
                try:
                    existing_identity = (
                        LocalFilesystemAdapter.regular_file_identity(existing_path)
                    )
                except (LocalContentChanged, LocalStorageError, OSError) as exc:
                    raise LocalMediaServiceError(
                        f"待替换目标已变化，请重新生成预览: {existing_path.name}"
                    ) from exc
                if existing_path == target:
                    expected_target_identity = existing_identity
                else:
                    retire_target = existing_path
                    expected_retire_identity = existing_identity

            group_id = (
                f"{match.media_type}:{planner._match_identity_key(match)}:"
                f"{plan.source_group_path or plan.original_path or '__root__'}"
            )
            local_plans.append(LocalMovePlan(
                snapshot,
                target,
                "video",
                group_id,
                action=action,
                note=note,
                provider=target_config.provider,
                library_id=target_config.library_id,
                library_name=target_config.library_name,
                local_target_root=target_config.path,
                server_path=target_config.server_path,
                expected_target_identity=expected_target_identity,
                retire_target=retire_target,
                expected_retire_identity=expected_retire_identity,
            ))

            for subtitle_plan in planning_result.subtitle_plans_by_video.get(
                str(plan.file_id), []
            ):
                subtitle_snapshot = snapshots_by_id.get(
                    str(subtitle_plan.file.file_id)
                )
                if subtitle_snapshot is None:
                    continue
                subtitle_target = destination_dir / subtitle_plan.target_name(
                    plan.new_name
                )
                subtitle_action = "skip" if action == "skip" else "move"
                if subtitle_action != "skip":
                    if subtitle_target in seen_targets:
                        raise LocalMediaServiceError(
                            f"本地整理计划包含重复字幕目标: {subtitle_target.name}"
                        )
                    seen_targets.add(subtitle_target)
                subtitle_note = (
                    "对应视频保留现有版本，字幕不移动"
                    if action == "skip" else ""
                )
                subtitle_target_identity = None
                try:
                    subtitle_target_identity = (
                        LocalFilesystemAdapter.regular_file_identity(subtitle_target)
                    )
                except (LocalContentChanged, LocalStorageError, OSError):
                    pass
                if subtitle_target_identity is not None and action != "skip":
                    subtitle_action = "replace"
                    subtitle_note = "随视频替换现有字幕"
                local_plans.append(LocalMovePlan(
                    subtitle_snapshot,
                    subtitle_target,
                    "subtitle",
                    group_id,
                    action=subtitle_action,
                    note=subtitle_note,
                    provider=target_config.provider,
                    library_id=target_config.library_id,
                    library_name=target_config.library_name,
                    local_target_root=target_config.path,
                    server_path=target_config.server_path,
                    expected_target_identity=(
                        subtitle_target_identity
                        if subtitle_action == "replace" else None
                    ),
                ))

            mapping = episode_mappings.get(str(plan.file_id))
            matches.append({
                "tmdb_id": match.tmdb_id,
                "title": match.title,
                "year": match.year,
                "media_type": match.media_type,
                "confidence": match.confidence,
                "provider": planner._match_provider(match),
                "external_id": planner._match_external_id(match),
                "source_name": snapshot.path.name,
                "target_name": plan.new_name,
                "season": plan.season,
                "episode": plan.episode,
                "source_season": plan.source_season,
                "source_episode": plan.source_episode,
                "episode_mapping": (
                    mapping.to_dict()
                    if mapping is not None
                    else (
                        plan.episode_mapping.to_dict()
                        if plan.episode_mapping is not None else {}
                    )
                ),
                "category": plan.main_category,
                "target_root": target_config.path,
            })

        if not local_plans and pending_confirmations:
            first = pending_confirmations[0]
            candidates = first.get("candidates") or []
            return {
                "inspection_id": inspection_id,
                "status": "requires_manual",
                "reason": first.get("reason") or "媒体识别结果需要人工确认",
                "candidate": first.get("candidate") or {},
                "candidates": candidates,
                "pending_confirmations": pending_confirmations,
                "files": [{"name": item.path.name} for item in videos],
                "snapshot_digest": inspection.digest,
                "plans": [],
                "cloud_write": False,
                "rules_snapshot": effective_rules_snapshot,
                "numbering_mode": normalized_numbering_mode,
            }

        return {
            "inspection_id": inspection_id,
            "status": "planned",
            "digest": inspection.digest,
            "cloud_write": False,
            "rules_snapshot": effective_rules_snapshot,
            "numbering_mode": normalized_numbering_mode,
            "position_overrides": {
                "season": season_override,
                "episode": episode_override,
            },
            "numbering": {
                "mode": normalized_numbering_mode,
                "changed": sum(
                    1 for item in episode_mappings.values() if item.changed
                ),
            },
            "matches": matches,
            "pending_confirmations": pending_confirmations,
            "planning_stats": planning_stats,
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
                for item in local_plans
            ],
            "subtitle_skipped": [
                {"reason": str(reason)}
                for reason in planning_stats.get("subtitle_reasons", [])
            ],
            "cleanup": [
                {
                    "name": item.snapshot.path.name,
                    "reason": item.reason,
                    "reason_code": item.reason_code,
                }
                for item in cleanup_candidates
            ],
            "retained": [
                {
                    "name": path.name,
                    "reason": "疑似 sample/proof 视频，已保留且不自动归档",
                    "reason_code": "sample-review",
                }
                for path in sorted(retained_sample_paths)
            ],
            "_move_plans": local_plans,
            "_cleanup_candidates": cleanup_candidates,
            "_retained_paths": [
                str(path) for path in sorted(retained_sample_paths)
            ],
        }

    @staticmethod
    def _refresh_paths(paths: set[str]) -> list[str]:
        """未绑定目标也只持久入队；实际刷新由统一合并器执行。"""
        from app.modules.media_refresh_coordinator import enqueue_media_refresh_paths

        try:
            results = enqueue_media_refresh_paths(sorted(paths))
        except Exception as exc:
            return [f"媒体库刷新入队失败: {exc}"]
        warnings: list[str] = []
        for label, status in results.items():
            if status == "failed":
                warnings.append(f"{label} 刷新入队失败")
        return warnings

    @staticmethod
    def _refresh_plans(plans: list[LocalMovePlan]) -> list[str]:
        """校验媒体库绑定后，按目标路径执行库内精准刷新。"""
        warnings: list[str] = []
        bound_paths: dict[tuple[str, str, str], set[str]] = {}
        unbound_paths: set[str] = set()
        for item in plans:
            provider = item.provider.strip().lower()
            library_id = item.library_id.strip()
            library_name = item.library_name.strip()
            target_parent = str(item.target.parent)
            if not provider:
                unbound_paths.add(target_parent)
                continue
            if not library_id and not library_name:
                warnings.append(f"{provider} 目标缺少媒体库绑定，已跳过刷新")
                continue
            server_path = str(getattr(item, "server_path", "") or "").strip()
            if server_path:
                local_root = str(getattr(item, "local_target_root", "") or "").strip()
                try:
                    target_parent = MediaServerPathMapping(
                        local_root, server_path,
                    ).apply(target_parent)
                except ValueError as exc:
                    warnings.append(
                        f"{provider} 服务端路径映射无效，已跳过刷新: {exc}"
                    )
                    continue
            bound_paths.setdefault(
                (provider, library_id, library_name), set(),
            ).add(target_parent)

        if unbound_paths:
            warnings.extend(LocalMediaService._refresh_paths(unbound_paths))
        if not bound_paths:
            return warnings

        from app.modules.media_server_profiles import list_configured_profiles
        profiles = {
            item.server_type: item for item in list_configured_profiles()
            if item.enabled and item.configured
        }
        clients: dict[str, Any] = {}
        folders_by_provider: dict[str, list[dict[str, Any]]] = {}
        for (provider, library_id, library_name), paths in sorted(bound_paths.items()):
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
                        client = JellyfinClient(
                            profile.url,
                            profile.credential,
                            **configured_media_server_refresh_options("jellyfin"),
                        )
                    elif provider == "emby":
                        from app.clients.emby import EmbyClient
                        client = EmbyClient(
                            profile.url,
                            profile.credential,
                            **configured_media_server_refresh_options("emby"),
                        )
                    else:
                        warnings.append(f"不支持的媒体服务器: {provider}")
                        continue
                    clients[provider] = client

                folders = folders_by_provider.get(provider)
                if folders is None:
                    folders = client.list_virtual_folders()
                    folders_by_provider[provider] = folders

                if library_id:
                    matches = [
                        item for item in folders
                        if str(item.get("id") or "").strip() == library_id
                    ]
                    if len(matches) != 1:
                        warnings.append(
                            f"{profile.label} 媒体库绑定已失效，请重新绑定: {label}"
                        )
                        continue
                    selected = matches[0]
                    actual_name = str(selected.get("name") or "").strip()
                    if (
                        library_name
                        and actual_name.casefold() != library_name.casefold()
                    ):
                        warnings.append(
                            f"{profile.label} 媒体库名称与绑定 ID 不一致，请重新绑定: "
                            f"{library_name}"
                        )
                        continue
                else:
                    matches = [
                        item for item in folders
                        if str(item.get("name") or "").strip().casefold()
                        == library_name.casefold()
                    ]
                    if len(matches) != 1:
                        reason = "不存在" if not matches else "存在同名媒体库"
                        warnings.append(f"{profile.label} {reason}，请重新绑定: {library_name}")
                        continue
                    selected = matches[0]

                resolved_id = str(selected.get("id") or "").strip()
                from app.modules.media_refresh_coordinator import (
                    enqueue_media_refresh_paths,
                )

                queued = enqueue_media_refresh_paths(
                    sorted(paths),
                    providers=(provider,),
                    allowed_library_ids=(resolved_id,),
                )
                queue_label = "Jellyfin" if provider == "jellyfin" else "Emby"
                if queued.get(queue_label) != "queued":
                    warnings.append(f"{profile.label} 刷新入队失败 {label}")
            except Exception as exc:
                warnings.append(f"{profile.label} 刷新失败 {label}: {exc}")
        for client in clients.values():
            close_media_server_client(client)
        return warnings

    @staticmethod
    def _committed_move_failure_message(
        exc: Exception,
        moved_targets: list[str],
        *,
        rollback_incomplete: bool = False,
        qb_task_retired: bool = False,
    ) -> str:
        if rollback_incomplete:
            prefix = "本地媒体移动失败且回滚不完整，文件状态需要人工核验"
        elif qb_task_retired and not moved_targets:
            prefix = "qB 任务已完成移除，但本地媒体后续收尾失败，已禁止自动恢复 qB"
        else:
            prefix = "本地媒体文件已完成移动，但后续收尾失败，已禁止自动恢复 qB"
        error_text = str(exc or type(exc).__name__).strip() or type(exc).__name__
        message = f"{prefix}：{error_text}"
        if moved_targets:
            visible = moved_targets[:10]
            suffix = "、".join(visible)
            if len(moved_targets) > len(visible):
                suffix += f" 等 {len(moved_targets)} 项"
            message += f"；已移动目标：{suffix}"
        return message[:4000]

    @staticmethod
    def _persist_committed_move_failure(
        owner: str, task_id: int, diagnostic: str,
    ) -> None:
        try:
            db.update_local_media_task(
                task_id,
                owner=owner,
                status="requires_manual",
                warning=diagnostic,
                error=diagnostic,
                completed_at=None,
            )
        except Exception as persist_exc:
            # 原始失败可能正是数据库瞬时异常；仍尽最大努力记录，且绝不能
            # 因二次落库失败转而恢复已提交移动任务的 qB。
            logger.error(
                "持久化本地媒体提交后异常失败 task=%s type=%s",
                task_id,
                type(persist_exc).__name__,
            )

    @_local_media_operation
    def prepare_task(self, owner: str, task_id: int) -> dict[str, Any]:
        """只读预热一个任务的识别与探测缓存，不改变任务状态或文件。"""
        task = db.get_local_media_task(task_id, owner=owner)
        if task is None:
            raise LocalMediaServiceError("本地媒体任务不存在")
        source = db.get_local_media_source(task.source_id, owner=owner)
        if source is None:
            raise LocalMediaServiceError("本地媒体来源不存在")
        if source.media_type == "nsfw":
            raise LocalMediaServiceError("成人番号来源保持串行识别")
        inspection_id = ""
        try:
            inspection = self.inspect_source(
                owner, task.source_id, task.content_path,
            )
            inspection_id = str(inspection.get("inspection_id") or "")
            preview = self.preview(
                owner, inspection_id, task.tmdb_id, task.media_type,
                rules_snapshot=task.rules_snapshot,
                automatic=task.trigger in {"scan", "qb_completed"},
                season_override=task.season_override,
                episode_override=task.episode_override,
                numbering_mode=task.numbering_mode,
            )
            return {
                "task_id": int(task_id),
                "status": str(preview.get("status") or ""),
                "pending_confirmations": len(
                    preview.get("pending_confirmations") or []
                ),
            }
        finally:
            if inspection_id:
                self.inspections.discard(owner, inspection_id)

    @staticmethod
    def _assert_no_active_task_path_overlap(owner: str, selected_path: Path) -> None:
        with db.get_conn() as conn:
            rows = conn.execute(
                "SELECT id,content_path FROM local_media_tasks "
                "WHERE owner=? AND status NOT IN ('completed','failed') ORDER BY id",
                (str(owner or "").strip(),),
            ).fetchall()
        for row in rows:
            try:
                task_path = Path(canonical_local_media_content_path(row["content_path"]))
            except ValueError:
                continue
            if local_media_paths_overlap(selected_path, task_path):
                raise LocalMediaServiceError(
                    "该条目与未完成的本地媒体任务路径重叠，请先完成或清理相关任务"
                )

    @_local_media_operation
    def move_media_item_to_trash(
        self,
        owner: str,
        source_id: int,
        path: Path | str,
        expected_identity: dict[str, Any],
    ) -> Path:
        """在统一本地媒体 Writer 内把一级媒体条目移入可恢复回收区。"""
        with _local_media_write_lease():
            source = db.get_local_media_source(int(source_id), owner=owner)
            if source is None:
                raise LocalMediaServiceError("本地媒体来源不存在")
            root = require_container_absolute_path(source.local_root, label="来源目录")
            selected = assert_within(Path(path), root)
            if selected == root or selected.parent != root:
                raise LocalMediaServiceError("仅允许删除来源根目录下的一级媒体条目")
            self._assert_no_active_task_path_overlap(owner, selected)
            return move_candidate_to_trash(source, selected, expected_identity)

    @_local_media_operation
    def execute_task(self, owner: str, task_id: int, *, qb_client=None) -> dict[str, Any]:
        with _local_media_write_lease():
            return self._execute_task_under_writer(
                owner, task_id, qb_client=qb_client,
            )

    def _execute_task_under_writer(
        self, owner: str, task_id: int, *, qb_client=None,
    ) -> dict[str, Any]:
        task = db.get_local_media_task(task_id, owner=owner)
        if task is None:
            raise LocalMediaServiceError("本地媒体任务不存在")
        source = db.get_local_media_source(task.source_id, owner=owner)
        if source is None:
            raise LocalMediaServiceError("本地媒体来源不存在")
        rules = self._restore_rules_snapshot(task.rules_snapshot) if task.rules_snapshot else OrganizeRules.from_config()
        paused = False
        move_committed = False
        qb_task_retired = False
        committed_targets: list[str] = []
        try:
            if task.qb_hash and qb_client is None:
                raise LocalMediaServiceError("qB 任务缺少可用客户端，已拒绝移动源文件")
            inspection = self.inspect_source(owner, task.source_id, task.content_path)
            preview = self.preview(
                owner, inspection["inspection_id"], task.tmdb_id, task.media_type,
                rules_snapshot=task.rules_snapshot,
                automatic=task.trigger in {"scan", "qb_completed"},
                season_override=task.season_override, episode_override=task.episode_override,
                numbering_mode=task.numbering_mode,
            )
            if preview.get("status") != "planned":
                db.update_local_media_task(
                    task_id, owner=owner, status="requires_manual",
                    snapshot_digest=str(preview.get("digest") or inspection.get("digest") or ""),
                    error=str(preview.get("reason") or "TMDB 结果需要人工确认"),
                )
                return {"status": "requires_manual", "task_id": task_id, "preview": preview}
            pending_confirmations = list(preview.get("pending_confirmations") or [])
            if pending_confirmations:
                reason = (
                    f"仍有 {len(pending_confirmations)} 组媒体需要人工确认；"
                    "为保证本地文件事务完整，本次尚未移动任何文件"
                )
                db.update_local_media_task(
                    task_id,
                    owner=owner,
                    status="requires_manual",
                    snapshot_digest=str(preview.get("digest") or inspection.get("digest") or ""),
                    error=reason,
                    completed_at=None,
                )
                return {
                    "status": "requires_manual",
                    "task_id": task_id,
                    "preview": preview,
                    "reason": reason,
                }
            for plan in preview["_move_plans"]:
                db.add_local_media_task_item(
                    task_id, str(plan.source.path), str(plan.target), role=plan.role,
                    media_group=plan.media_group, action=plan.action, size=plan.source.size,
                    mtime_ns=plan.source.mtime_ns, device=plan.source.device, inode=plan.source.inode,
                    owner=owner,
                )
            recognition = build_recognition_summary(preview.get("matches", []))
            recognition_fields: dict[str, object] = {
                "status": "planned",
                "error": "",
                "snapshot_digest": str(preview.get("digest") or inspection.get("digest") or ""),
                "recognition_summary": serialize_recognition_summary(recognition),
            }
            resolved_media = recognition.get("media") if recognition.get("status") == "resolved" else None
            if isinstance(resolved_media, list) and len(resolved_media) == 1:
                primary = resolved_media[0]
                if isinstance(primary, dict):
                    recognition_fields.update({
                        "tmdb_id": str(primary.get("tmdb_id") or ""),
                        "title": str(primary.get("title") or ""),
                        "year": str(primary.get("year") or ""),
                        "media_type": str(primary.get("media_type") or ""),
                    })
            db.update_local_media_task(task_id, owner=owner, **recognition_fields)
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
                # 锁序固定：本地媒体 pipeline writer（外层）-> qB writer。
                # moving 状态与真实暂停请求同属一个 qB 临界区；Web 恢复/删除
                # 在获取 lease 后会看到 moving 并拒绝，消除检查/调用间竞态。
                with qb_control_write_lease():
                    db.update_local_media_task(task_id, owner=owner, status="moving")
                    qb_client.pause_torrents(task.qb_hash)
                    paused = True
            else:
                db.update_local_media_task(task_id, owner=owner, status="moving")
            executable_plans = [item for item in preview["_move_plans"] if item.action != "skip"]
            skipped_plans = [item for item in preview["_move_plans"] if item.action == "skip"]
            if executable_plans:
                result = LocalMoveTransaction(
                    [Path(source.local_root)],
                    [Path(item.path) for item in db.list_local_library_targets(task.source_id, owner=owner)],
                    task_id=task_id, owner=owner, operation_token=task.operation_token,
                ).execute(executable_plans)
                # LocalMoveTransaction 只有在全部移动完成并完成内部校验后才返回。
                # 从此处开始，任何异常都属于提交后收尾失败，绝不能恢复 qB。
                move_committed = True
                committed_targets = [str(item.target) for item in result.moved]
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
                    with qb_control_write_lease():
                        qb_client.delete_torrents(task.qb_hash, delete_files=False)
                        qb_task_retired = True
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
                media_refresh_status = "failed" if refresh_warnings else "queued"
            final_status = (
                "requires_manual" if result.status == "requires_manual" else "completed"
            )
            db.update_local_media_task(
                task_id, owner=owner, status=final_status, warning="；".join(warnings),
                completed_at=db.now(), error="",
            )
            return {
                "status": final_status, "task_id": task_id,
                "moved": [str(item.target) for item in result.moved],
                "deleted_junk": list(cleanup_result.deleted) if cleanup_result else [],
                "retained_junk": [
                    *list(preview.get("_retained_paths") or []),
                    *(list(cleanup_result.retained) if cleanup_result else []),
                ],
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
            rollback_incomplete = bool(getattr(exc, "rollback_errors", None))
            if move_committed or qb_task_retired or rollback_incomplete:
                diagnostic = self._committed_move_failure_message(
                    exc,
                    committed_targets,
                    rollback_incomplete=rollback_incomplete,
                    qb_task_retired=qb_task_retired,
                )
                self._persist_committed_move_failure(owner, task_id, diagnostic)
                raise LocalMediaPostMoveError(diagnostic) from exc
            if paused and task.qb_hash and qb_client is not None:
                try:
                    with qb_control_write_lease():
                        qb_client.resume_torrents(task.qb_hash)
                except Exception as resume_exc:
                    exc = LocalMediaServiceError(f"{exc}；qB 恢复失败: {resume_exc}")
            try:
                db.update_local_media_task(
                    task_id, owner=owner, status="failed", error=str(exc)
                )
            except Exception as persist_exc:
                logger.error(
                    "持久化本地媒体提交前异常失败 task=%s type=%s",
                    task_id,
                    type(persist_exc).__name__,
                )
            raise

    @_local_media_operation
    def create_manual_task(
        self, owner: str, inspection_id: str, *, tmdb_id: str = "", media_type: str = "",
        rules_snapshot: str = "", season_override: int | None = None,
        episode_override: int | None = None, numbering_mode: str = "auto",
    ) -> int:
        inspection = self.inspections.get(owner, inspection_id)
        normalized_numbering_mode = self._normalize_numbering_mode(numbering_mode)
        season_override, episode_override = self._normalize_position_overrides(
            inspection, season_override, episode_override,
        )
        normalized_type = str(media_type or "").strip().lower()
        if normalized_type not in {"", "auto", "movie", "tv"}:
            raise LocalMediaServiceError("媒体类型必须是 auto、movie 或 tv")
        source = db.get_local_media_source(inspection.source_id, owner=owner)
        nsfw_only = source is not None and source.media_type == "nsfw"
        if nsfw_only and str(tmdb_id or "").strip():
            raise LocalMediaServiceError(
                "成人番号专用来源不能套用 TMDB 候选，只接受 MetaTube 精确番号结果"
            )
        effective_type = (
            "movie"
            if nsfw_only
            else normalized_type
            if normalized_type in {"movie", "tv"}
            else inspection.media_type or (source.media_type if source else "")
        )
        if effective_type not in {"movie", "tv"}:
            raise LocalMediaServiceError("无法确定媒体类型，请先选择电影或剧集")
        if (season_override is not None or episode_override is not None) and effective_type != "tv":
            raise LocalMediaServiceError("只有剧集整理可以指定季数或集数")
        if normalized_numbering_mode != "auto" and effective_type != "tv":
            raise LocalMediaServiceError("只有剧集整理可以指定编号方式")
        normalized_snapshot = ""
        if rules_snapshot:
            normalized_snapshot = self._serialize_rules_snapshot(
                self._restore_rules_snapshot(rules_snapshot)
            )
        task_id = db.prepare_manual_local_media_task(
            inspection.source_id, str(inspection.selected_path), owner=owner,
            tmdb_id=str(tmdb_id or "").strip(), media_type=effective_type,
            rules_snapshot=normalized_snapshot, season_override=season_override,
            episode_override=episode_override,
            numbering_mode=normalized_numbering_mode,
        )
        # 检查快照已转化为持久化任务，继续保留只会重复占用大量路径/
        # inode 快照内存。数据库写入成功后再消费，失败时仍允许用户重试。
        self.inspections.discard(owner, inspection_id)
        return task_id

    @_local_media_operation
    def execute_preview(
        self, owner: str, inspection_id: str, preview: dict[str, Any],
    ) -> MoveTransactionResult:
        with _local_media_write_lease():
            return self._execute_preview_under_writer(owner, inspection_id, preview)

    def _execute_preview_under_writer(
        self, owner: str, inspection_id: str, preview: dict[str, Any],
    ) -> MoveTransactionResult:
        inspection = self.inspections.get(owner, inspection_id)
        if preview.get("status") != "planned" or not preview.get("_move_plans"):
            raise LocalMediaServiceError("预览尚未达到可执行状态")
        if preview.get("pending_confirmations"):
            raise LocalMediaServiceError("预览仍有媒体需要人工确认，尚未移动任何文件")
        executable_plans = [
            item for item in preview["_move_plans"] if item.action != "skip"
        ]
        if not executable_plans:
            result = MoveTransactionResult(
                status="completed",
                warnings=["按冲突策略保留现有文件，没有需要移动的项目"],
            )
            self.inspections.discard(owner, inspection_id)
            return result
        targets = db.list_local_library_targets(inspection.source_id, owner=owner)
        transaction = LocalMoveTransaction(
            [inspection.root], [Path(item.path) for item in targets], owner=owner,
        )
        result = transaction.execute(executable_plans)
        self.inspections.discard(owner, inspection_id)
        return result


_service: LocalMediaService | None = None
_service_lock = threading.Lock()


def get_local_media_service() -> LocalMediaService:
    """返回可用实例，并收敛上一次未完成关闭留下的旧实例。"""
    global _service
    while True:
        with _service_lock:
            service = _service
            if service is None:
                service = _service = LocalMediaService()
                return service

        closed, closing, active = service._lifecycle_state()
        if not closed and not closing:
            return service
        if closing and active:
            raise LocalMediaServiceError("本地媒体运行时正在切换，请稍后重试")
        if not closed and not service.close():
            raise LocalMediaServiceError("本地媒体运行时尚未完成关闭，请稍后重试")
        with _service_lock:
            if _service is service:
                _service = None


def close_local_media_service() -> bool:
    """关闭并移除 Web/Agent 共用的本地媒体服务。"""
    global _service
    with _service_lock:
        service = _service
    if service is None:
        return True
    closed = service.close()
    if closed:
        with _service_lock:
            if _service is service:
                _service = None
    return closed
