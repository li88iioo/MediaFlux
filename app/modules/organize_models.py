"""统一整理的数据模型、调用上下文及阶段异常。

本模块不创建客户端、执行池、缓存或锁；上下文只引用协调器传入的运行态。
所有入口共享这些类型，避免扫描、规划与执行阶段出现平行模型。
"""
from __future__ import annotations

import threading
from concurrent.futures import Executor
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Callable, Mapping

from app.clients.guangya import GuangYaFile
from app.modules.episode_mapping import EpisodeMappingPlan
from app.modules.media_probe import MediaProfile
from app.modules.media_variant import MediaVariant
from app.modules.organize_groups import OrganizeGroupTask
from app.modules.organize_runtime import OrganizeTaskRuntime
from app.modules.organize_scan import OrganizeScanResult, ScanRestriction
from app.modules.scraper import MatchResult


class OrganizeScanUnsafeError(RuntimeError):
    """扫描快照不可信，整个来源必须失败关闭。

    这类错误与单个媒体组的运行期失败不同：部分快照会让后续清理和冲突
    仲裁失去依据，因此禁止被组级失败隔离吞掉。
    """


class _OrganizeAuditWriteError(RuntimeError):
    def __init__(self, log_id: int, cause: Exception):
        super().__init__(str(cause))
        self.log_id = log_id
        self.__cause__ = cause


@dataclass
class OrganizePlan:
    file_id: str
    original_name: str
    original_path: str
    original_parent_id: str = "0"
    size: int = 0
    etag: str = ""
    match: MatchResult | None = None
    main_category: str = ""
    region: str = ""
    year: str = ""
    season: int | None = None
    episode: int | None = None
    source_season: int | None = None
    source_episode: int | None = None
    episode_mapping: EpisodeMappingPlan | None = None
    base_name: str = ""
    new_name: str = ""
    variant: MediaVariant = field(default_factory=MediaVariant)
    variant_label: str = ""
    variant_suffix: str = ""
    conflict_decision: str = "new"
    conflict_note: str = ""
    target_path: str = ""
    media_root_path: str = ""
    identity_guard_required: bool = False
    backdrop_path: str = ""
    poster_path: str = ""
    season_total: int = 0
    action: str = "move"  # move / skip / conflict
    note: str = ""
    # 追加在末尾以保持历史位置参数构造的语义兼容。
    source_group_id: str = ""
    source_group_path: str = ""
    media_profile: MediaProfile | None = field(default=None, repr=False, compare=False)
    media_probe_complete: bool = False
    media_probe_pending: bool = False
    conflict_existing_id: str = ""
    conflict_existing_name: str = ""
    multipart_index: int | None = None
    multipart_token: str = ""
    multipart_ambiguous: bool = False


    def require_confirmation(self, reason: str, *, constraint: str = "") -> None:
        """将身份/位置拒绝统一投影到识别结果和执行计划，不覆盖位置证据。"""
        assert self.match is not None
        self.match.need_confirm = True
        self.match.status = "low_confidence"
        self.match.error = reason
        if constraint:
            rejected = getattr(self.match, "rejected_constraints", None)
            if isinstance(rejected, list) and constraint not in rejected:
                rejected.append(constraint)
        self.action = "skip"
        self.note = reason

    def mark_matched(self) -> None:
        """身份/位置证明通过后，清除先前的待确认状态；证据由调用方保留。"""
        assert self.match is not None
        self.match.need_confirm = False
        self.match.status = "matched"
        self.match.error = ""


@dataclass(frozen=True)
class OrganizeContext:
    """一次整理运行的上下文。

    业务规则由 :class:`OrganizeRules` 承载；这里仅保存运行时控制项，
    避免内部阶段继续传递一长串容易错位的参数。
    """

    source_dir_id: str
    dry_run: bool = True
    max_files: int = 0
    cancel_event: threading.Event | None = None
    post_actions: bool = True
    source_name: str = ""
    require_complete_scan: bool = False
    media_probe_cache_only: bool | None = None
    protected_source_ids: frozenset[str] = frozenset()
    # 来源根保护与下载隔离保护共用扫描边界；后者需额外报告被排除的范围。
    protected_staging_ids: frozenset[str] = frozenset()
    automatic: bool = False
    # 组级流水线的实时进度回调；只用于观测，异常不得影响整理结果。
    group_progress: Callable[[dict], None] | None = None
    # 显式选择整源快照规划；预览和 max_files 保持整源语义。
    group_pipeline: bool = True
    # 单次调用的审计归属键；用于精确回读本轮日志，避免并发任务污染。
    operation_token: str = ""
    # 来源适配器可提供显式媒体类型提示；普通光鸭整理保持空值，继续依赖
    # 文件名与目录上下文自动判断。本地来源配置和手动刮削可复用同一规划
    # 流水线，而不需要在规划器外再实现一套 match/parse 分支。
    media_type_hint: str = ""
    # 当前来源可见的只读规划 Worker 总预算。多来源调度传入共享执行池后，
    # 该值描述全局池大小，而不是为来源预先切分的固定份额。
    planning_workers: int | None = None
    # 当前来源可见的 ffprobe 总预算；真正并发量由 media_probe 的进程级
    # 槽位统一限制，因此多来源之间可以动态复用空闲预算。
    media_probe_workers: int | None = None
    # 多来源并行时共享的只读规划池。各来源只负责枚举并提交媒体单元，
    # Worker 完成短目录后可继续领取其他来源的大目录任务。
    planning_executor: Executor | None = field(default=None, repr=False, compare=False)
    # 多来源并行时共享的单写门。扫描、识别和探测不持有该锁；最终冲突
    # 仲裁、云盘写入、审计与清理必须在锁内完成。
    execution_lock: object | None = field(default=None, repr=False, compare=False)
    # 同一次后台整理跨来源/媒体组共享的短生命周期运行态。仅缓存已严格
    # 验证的作品身份与单写入器维护的目标库存，绝不跨任务持久化。
    task_runtime: OrganizeTaskRuntime | None = field(
        default=None, repr=False, compare=False,
    )

    # 服务端冻结的父生命周期/收件范围，必须早于 probe 入队。
    notification_context: dict | None = None
    # None 保留自动整理实时发现语义；空集合表示不接纳任何来源成员。
    source_member_ids: frozenset[str] | None = None
    # 人工确认时的原始对象版本；扫描和最终写入共用，禁止实时扫描重绑定。
    source_snapshots: Mapping[str, GuangYaFile] | None = None
    # 冻结选集整源扫描的只读结果；组级扫描只复用本组切片，不额外回读来源。
    source_entries: Mapping[str, tuple[GuangYaFile, ...]] | None = field(
        default=None, repr=False, compare=False,
    )

    def for_group(self, restriction: ScanRestriction, video_exts: set[str]) -> OrganizeContext:
        """按目录子树或根层媒体单元同时切片成员、版本约束及扫描结果。"""
        assert self.source_entries is not None and self.source_snapshots is not None
        entries_by_dir = {}
        member_ids = {restriction.dir_id}
        pending = [restriction.dir_id]
        while pending:
            dir_id = pending.pop()
            if dir_id in entries_by_dir:
                continue
            entries = self.source_entries[dir_id]
            if restriction.files_only:
                entries = tuple(
                    item for item in entries
                    if not item.is_dir and (
                        not restriction.file_ids or item.file_id in restriction.file_ids
                        or item.name.rsplit(".", 1)[-1].lower() not in video_exts
                    )
                )
            entries_by_dir[dir_id] = entries
            member_ids.update(item.file_id for item in entries)
            pending.extend(item.file_id for item in entries if item.is_dir)
        return replace(
            self, source_member_ids=frozenset(member_ids),
            source_snapshots=MappingProxyType({
                file_id: item for file_id, item in self.source_snapshots.items()
                if file_id in member_ids
            }),
            source_entries=MappingProxyType(entries_by_dir),
        )

    @property
    def probe_cache_only(self) -> bool:
        if self.media_probe_cache_only is None:
            return self.dry_run
        return self.media_probe_cache_only

    def cancelled(self) -> bool:
        return bool(self.cancel_event and self.cancel_event.is_set())


@dataclass
class OrganizePlanningResult:
    plans: list[OrganizePlan]
    subtitle_plans_by_video: dict[str, list]


@dataclass
class _PreparedOrganizeGroup:
    """媒体组只读规划结果；由 coordinator 按稳定顺序提交给单 Writer。"""

    task: OrganizeGroupTask
    stats: dict
    scan_result: OrganizeScanResult | None = None
    planning_result: OrganizePlanningResult | None = None
    planning_elapsed_seconds: float = 0.0
    error: Exception | None = field(default=None, repr=False)
