"""媒体组任务、结果模型与组级统计合并。

统一整理过去按“整源扫描 → 整源规划 → 整源执行”运行，第一个媒体目录必须
等待全部来源完成 TMDB/ffprobe 识别才能产生结果。本模块提供把来源拆分为
顶层媒体组任务所需的稳定模型，使调度层可以逐组完成扫描、识别与执行，
同时保留组内完整目录上下文与冲突预检。

设计约束：
- 只拆分到第一层媒体目录；Season/Specials 等深层目录继续归属所属作品组。
- 枚举失败必须显式标记，禁止用部分列表冒充完整枚举。
- 模型必须可安全序列化为 JSON，供任务状态、Web 与 TG 直接读取。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from app.logger import get_logger

logger = get_logger("app.modules.organize")

# 根目录直属媒体使用固定组身份，避免与真实目录名冲突。
GROUP_ROOT_PATH = "__root__"

GROUP_STATUS_PLANNED = "planned"
GROUP_STATUS_RUNNING = "running"
GROUP_STATUS_COMPLETED = "completed"
GROUP_STATUS_PARTIAL = "partial"
GROUP_STATUS_FAILED = "failed"
GROUP_STATUS_STOPPED = "stopped"

_TERMINAL_GROUP_STATUSES = frozenset({
    GROUP_STATUS_COMPLETED,
    GROUP_STATUS_PARTIAL,
    GROUP_STATUS_FAILED,
    GROUP_STATUS_STOPPED,
})

# 组级阶段，仅用于实时进度投影。
GROUP_STAGE_PENDING = "pending"
GROUP_STAGE_SCAN = "scan"
GROUP_STAGE_PLAN = "plan"
GROUP_STAGE_EXECUTE = "execute"
GROUP_STAGE_CLEANUP = "cleanup"
GROUP_STAGE_DONE = "done"

GROUP_STAGE_LABELS = {
    GROUP_STAGE_PENDING: "等待处理",
    GROUP_STAGE_SCAN: "扫描目录",
    GROUP_STAGE_PLAN: "识别与规划",
    GROUP_STAGE_EXECUTE: "执行整理",
    GROUP_STAGE_CLEANUP: "清理空目录",
    GROUP_STAGE_DONE: "已完成",
}


def group_key(group_id: str, group_path: str) -> str:
    """与 ``organize_execution`` 中的分组键保持完全一致。"""
    return f"{str(group_id or '')}\x1f{str(group_path or GROUP_ROOT_PATH)}"


@dataclass(frozen=True)
class OrganizeGroupTask:
    """一个可独立扫描、识别并执行的媒体组任务。"""

    source_dir_id: str
    source_name: str
    group_id: str
    group_path: str
    group_name: str
    index: int = 0
    total: int = 0
    is_root: bool = False
    etag: str = ""
    updated_at: int = 0
    trigger: str = "manual"
    # 根目录可被拆成多个独立媒体单元；空元组表示沿用历史的整层扫描。
    file_ids: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return group_key(self.group_id, self.group_path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_dir_id": self.source_dir_id,
            "source_name": self.source_name,
            "group_id": self.group_id,
            "group_path": self.group_path,
            "group_name": self.group_name,
            "index": int(self.index),
            "total": int(self.total),
            "is_root": bool(self.is_root),
            "trigger": self.trigger,
            "file_ids": list(self.file_ids),
        }


@dataclass
class OrganizeGroupResult:
    """单个媒体组的结构化结果，禁止依赖自由格式日志反向解析。"""

    task: OrganizeGroupTask
    status: str = GROUP_STATUS_PLANNED
    stage: str = GROUP_STAGE_PENDING
    identities: list[str] = field(default_factory=list)
    scanned: int = 0
    moved: int = 0
    replaced: int = 0
    metadata_moved: int = 0
    skipped: int = 0
    need_confirm: int = 0
    failed: int = 0
    failures: list[str] = field(default_factory=list)
    changed_target_dirs: list[str] = field(default_factory=list)
    changed_strm_paths: list[str] = field(default_factory=list)
    strm_changes: list[dict[str, Any]] = field(default_factory=list)
    cleanup: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    elapsed_seconds: float = 0.0
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """仅输出可安全 JSON 序列化的字段；原始 stats 由调用方按需保留。"""
        return {
            **self.task.to_dict(),
            "key": self.task.key,
            "status": self.status,
            "stage": self.stage,
            "identities": list(self.identities),
            "scanned": int(self.scanned),
            "moved": int(self.moved),
            "replaced": int(self.replaced),
            "metadata_moved": int(self.metadata_moved),
            "skipped": int(self.skipped),
            "need_confirm": int(self.need_confirm),
            "failed": int(self.failed),
            "failures": list(self.failures),
            "changed_target_dirs": list(self.changed_target_dirs),
            "changed_strm_paths": list(self.changed_strm_paths),
            "cleanup": dict(self.cleanup),
            "error": self.error,
            "elapsed_seconds": round(float(self.elapsed_seconds), 3),
        }

    def progress_row(self) -> dict[str, Any]:
        """`stats["source_groups"]` 行格式，保持既有前端字段兼容。"""
        return {
            "id": self.task.group_id,
            "path": self.task.group_path,
            "name": self.task.group_name,
            "status": self.status,
            "stage": self.stage,
            "index": int(self.task.index),
            "total": int(self.scanned),
            "moved": int(self.moved),
            "metadata_moved": int(self.metadata_moved),
            "skipped": int(self.skipped),
            "need_confirm": int(self.need_confirm),
            "failed": int(self.failed),
        }


@dataclass
class GroupEnumeration:
    """媒体组枚举结果。``complete=False`` 时禁止任何删除型后处理。"""

    tasks: list[OrganizeGroupTask] = field(default_factory=list)
    complete: bool = True
    errors: list[str] = field(default_factory=list)
    source_root_name: str = ""

    def __bool__(self) -> bool:
        return bool(self.tasks)


def _video_extension(name: str) -> str:
    text = str(name or "")
    return text.rsplit(".", 1)[-1].lower() if "." in text else ""


_EXPLICIT_EPISODE_TOKEN_RE = re.compile(
    r"(?ix)(?:"
    r"(?<![A-Z0-9])S(?P<s1>\d{1,2})[\s._-]*E(?:P)?(?P<e1>\d{1,4})"
    r"|(?<!\d)(?P<s2>\d{1,2})x(?P<e2>\d{1,4})(?!\d)"
    r"|(?<![A-Z0-9])SEASON[\s._-]*(?P<s3>\d{1,2})[\s._-]*"
    r"(?:EPISODE|EP|E)[\s._-]*(?P<e3>\d{1,4})"
    r"|第[\s._-]*(?P<s4>\d{1,2})[\s._-]*季.*?第[\s._-]*"
    r"(?P<e4>\d{1,4})[\s._-]*(?:集|话|話)"
    r"|第[\s._-]*(?P<e5>\d{1,4})[\s._-]*(?:集|话|話)"
    r"|(?<![A-Z0-9])E(?:P)?[\s._-]*(?P<e6>\d{1,4})(?!\d)"
    r")"
)
_EXPLICIT_MULTIPART_TOKEN_RE = re.compile(
    r"(?i)(?:^|[\s._-])(?:CD|DISC|DISK|PART|PT)[\s._-]*(\d{1,2})"
    r"(?=$|[\s._-])"
)
_PLAIN_EPISODE_TOKEN_RE = re.compile(
    r"(?i)\s+-\s+(?:\d{1,4}|OVA(?:\s*\d{1,3})?|ONA(?:\s*\d{1,3})?|"
    r"SP(?:ECIAL)?(?:\s*\d{1,3})?)(?=$|[\s._\-\[(])"
)
_MOVIE_RELEASE_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_UNIT_KEY_NOISE_RE = re.compile(r"[^0-9a-z\u3400-\u9fff]+", re.IGNORECASE)


def _unit_key_text(value: str) -> str:
    """生成仅用于同批分组的保守比较键，不参与最终媒体识别。"""
    stem = str(value or "").rsplit(".", 1)[0]
    return _UNIT_KEY_NOISE_RE.sub(" ", stem.casefold()).strip()


def _movie_release_unit_key(name: str) -> str:
    """用“片名 + 年份”保守合并同一电影的多个规格版本。"""
    stem = str(name or "").rsplit(".", 1)[0]
    years = list(_MOVIE_RELEASE_YEAR_RE.finditer(stem))
    if not years:
        return ""
    # 标题本身可能含年份（例如 2001），发布年份通常是最后一个年份标记。
    year = years[-1]
    title = _unit_key_text(stem[:year.start()])
    if not title:
        # ``2026.Movie`` 一类年份前置名称无法仅靠轻量枚举可靠取标题，
        # 失败关闭为单文件，避免把同年不同作品错误合并。
        return ""
    return f"movie:{title}:{year.group(0)}"


def _root_media_unit_key(item, *, nsfw_enabled: bool = False) -> str:
    """把明确属于同一作品的根目录文件保留在一个规划单元。"""
    name = str(getattr(item, "name", "") or "")
    identifier = None
    if nsfw_enabled:
        try:
            from app.modules.nsfw import extract_nsfw_identifier, normalize_code

            identifier = extract_nsfw_identifier(name)
        except Exception:
            identifier = None
    if identifier is not None:
        code = normalize_code(identifier.code)
        if code:
            return f"code:{code}"

    episode = _EXPLICIT_EPISODE_TOKEN_RE.search(name)
    if episode is not None:
        title = _unit_key_text(name[:episode.start()])
        if title:
            season = next(
                (
                    int(value)
                    for value in (
                        episode.group("s1"), episode.group("s2"),
                        episode.group("s3"), episode.group("s4"),
                    )
                    if value is not None
                ),
                -1,
            )
            return f"tv:{title}:s{season}"

    stem = name.rsplit(".", 1)[0]
    plain_episode = _PLAIN_EPISODE_TOKEN_RE.search(stem)
    if plain_episode is not None:
        tail = stem[plain_episode.end():]
        # ``Movie - 2.2026`` 更像续作而非第 2 集；年份位于数字后方时
        # 不做轻量季包推断，交给单文件识别。
        if not _MOVIE_RELEASE_YEAR_RE.search(tail):
            title = _unit_key_text(stem[:plain_episode.start()])
            if title:
                return f"tvplain:{title}"

    multipart = _EXPLICIT_MULTIPART_TOKEN_RE.search(stem)
    if multipart is not None:
        base = _unit_key_text(f"{stem[:multipart.start()]} {stem[multipart.end():]}")
        if base:
            return f"multipart:{base}"

    movie = _movie_release_unit_key(name)
    if movie:
        return movie

    file_id = str(getattr(item, "file_id", "") or "").strip()
    return f"file:{file_id}"


def _root_media_units(
    root_videos: list[object], *, nsfw_enabled: bool = False,
) -> list[list[object]] | None:
    """安全拆分根目录媒体；身份不完整时返回 ``None`` 触发旧路径。"""
    if not root_videos:
        return []
    file_ids = [str(getattr(item, "file_id", "") or "").strip() for item in root_videos]
    if any(not file_id for file_id in file_ids) or len(set(file_ids)) != len(file_ids):
        return None
    units: dict[str, list[object]] = {}
    for item in root_videos:
        units.setdefault(_root_media_unit_key(item, nsfw_enabled=nsfw_enabled), []).append(item)
    return list(units.values())


def _root_group_name(items: list[object], fallback: str) -> str:
    if len(items) != 1:
        first = str(getattr(items[0], "name", "") or "").rsplit(".", 1)[0].strip()
        return first or fallback
    return str(getattr(items[0], "name", "") or "").rsplit(".", 1)[0].strip() or fallback


def enumerate_group_tasks(
    client,
    *,
    source_dir_id: str,
    source_name: str = "",
    video_exts: set[str] | frozenset[str],
    protected_source_ids: set[str] | frozenset[str] = frozenset(),
    trigger: str = "manual",
    nsfw_enabled: bool = False,
    cancelled: Callable[[], bool] | None = None,
) -> GroupEnumeration:
    """只列举根目录直属媒体与第一层媒体目录，不触发识别或探测。

    根目录直属视频按明确媒体身份拆成独立规划单元；季包、同番号和明确
    CD/Part 分段仍保持为同一单元。无法可靠拆分时回退一个 ``__root__`` 组。
    每个第一层子目录构成独立媒体组，深层 Season/Specials 继续继承本组身份。
    """
    source_dir_id = str(source_dir_id or "").strip()
    protected = {
        str(item).strip() for item in (protected_source_ids or set()) if str(item).strip()
    }
    protected.discard(source_dir_id)
    root_name = str(source_name or "").strip()

    try:
        info = client.file_info(source_dir_id)
        if info is not None and getattr(info, "name", ""):
            root_name = str(info.name).strip() or root_name
    except Exception:
        # 来源显示名缺失不影响拆分正确性，扫描阶段还会再次尝试补齐。
        pass

    try:
        entries = client.list_dir(source_dir_id)
    except Exception as exc:
        logger.error(
            "媒体组枚举失败 source=%s type=%s", source_dir_id, type(exc).__name__
        )
        return GroupEnumeration(
            tasks=[],
            complete=False,
            errors=[f"{source_dir_id}: 目录读取失败"],
            source_root_name=root_name,
        )

    if cancelled is not None and cancelled():
        return GroupEnumeration(
            tasks=[], complete=False, errors=["枚举已取消"], source_root_name=root_name,
        )

    exts = {str(item).lower() for item in (video_exts or set())}
    tasks: list[OrganizeGroupTask] = []
    root_videos = [
        item
        for item in entries
        if not getattr(item, "is_dir", False)
        and _video_extension(getattr(item, "name", "")) in exts
    ]
    root_units = _root_media_units(root_videos, nsfw_enabled=nsfw_enabled)
    if root_videos and (root_units is None or len(root_units) <= 1):
        # 单一媒体单元和身份异常场景保持历史根组身份；后者不带过滤条件，
        # 失败关闭为整层扫描，避免缺失/重复 file_id 导致漏整理。
        only_unit = root_units[0] if root_units else []
        tasks.append(OrganizeGroupTask(
            source_dir_id=source_dir_id,
            source_name=root_name,
            group_id=source_dir_id,
            group_path=GROUP_ROOT_PATH,
            group_name=root_name or source_dir_id,
            is_root=True,
            trigger=trigger,
            file_ids=(
                tuple(str(getattr(item, "file_id", "")) for item in only_unit)
                if root_units is not None
                else ()
            ),
        ))
    elif root_units:
        for unit in root_units:
            file_ids = tuple(str(getattr(item, "file_id", "")) for item in unit)
            tasks.append(OrganizeGroupTask(
                source_dir_id=source_dir_id,
                source_name=root_name,
                group_id=file_ids[0],
                group_path=GROUP_ROOT_PATH,
                group_name=_root_group_name(unit, root_name or source_dir_id),
                is_root=True,
                trigger=trigger,
                file_ids=file_ids,
            ))

    seen_dirs: set[str] = set()
    for item in entries:
        if not getattr(item, "is_dir", False):
            continue
        child_id = str(getattr(item, "file_id", "") or "").strip()
        if not child_id or child_id in protected or child_id in seen_dirs:
            continue
        seen_dirs.add(child_id)
        name = str(getattr(item, "name", "") or "").strip()
        if not name:
            continue
        try:
            updated_at = max(0, int(getattr(item, "updated_at", 0) or 0))
        except (TypeError, ValueError):
            updated_at = 0
        tasks.append(OrganizeGroupTask(
            source_dir_id=source_dir_id,
            source_name=root_name,
            group_id=child_id,
            group_path=name,
            group_name=name,
            etag=str(getattr(item, "etag", "") or ""),
            updated_at=updated_at,
            trigger=trigger,
        ))

    total = len(tasks)
    if not total:
        # 空来源仍必须产生一个根组：执行阶段的清理安全门、审计与后处理
        # 顺序都依赖“至少运行一次”，否则空目录来源会静默跳过全部检查。
        tasks.append(OrganizeGroupTask(
            source_dir_id=source_dir_id,
            source_name=root_name,
            group_id=source_dir_id,
            group_path=GROUP_ROOT_PATH,
            group_name=root_name or source_dir_id,
            is_root=True,
            trigger=trigger,
        ))
        total = 1
    tasks = [
        replace(task, index=position, total=total)
        for position, task in enumerate(tasks, start=1)
    ]
    return GroupEnumeration(tasks=tasks, complete=True, source_root_name=root_name)


# ===== 组级统计合并 =====

# 由流水线自行维护，禁止按组累加或覆盖。
_PIPELINE_OWNED_KEYS = frozenset({
    "source_groups",
    "source_groups_total",
    "source_groups_completed",
    "current_source_group",
    "group_progress",
    "group_results",
})

# 标志位语义为“任一组命中即成立”，累加会得到无意义的计数。
_MAX_INT_KEYS = frozenset({
    "stopped",
    "scan_limited",
    "empty_dir_cleanup_skipped",
})

# 全部组均完整才算完整。
_AND_BOOL_KEYS = frozenset({"scan_complete"})

# 去重且限长的原因类列表。
_REASON_LIST_LIMITS = {
    "confirmations": 3,
    "skip_reasons": 20,
    "subtitle_reasons": 20,
    "scan_errors": 20,
    "empty_dir_cleanup_reasons": 6,
}


def merge_group_stats(aggregate: dict, group_stats: dict) -> dict:
    """把单组统计合并进任务级统计。

    合并策略按键类型固定，避免出现“后一组覆盖前一组”的静默丢数据。
    """
    for key, value in (group_stats or {}).items():
        if key in _PIPELINE_OWNED_KEYS:
            continue
        if isinstance(value, bool):
            if key in _AND_BOOL_KEYS:
                previous = aggregate.get(key)
                aggregate[key] = bool(value) if previous is None else bool(previous and value)
            else:
                aggregate[key] = bool(aggregate.get(key)) or bool(value)
            continue
        if isinstance(value, int):
            if key in _MAX_INT_KEYS:
                aggregate[key] = max(int(aggregate.get(key, 0) or 0), int(value))
            else:
                aggregate[key] = int(aggregate.get(key, 0) or 0) + int(value)
            continue
        if isinstance(value, float):
            aggregate[key] = round(float(aggregate.get(key, 0.0) or 0.0) + float(value), 3)
            continue
        if isinstance(value, list):
            limit = _REASON_LIST_LIMITS.get(key)
            bucket = aggregate.setdefault(key, [])
            if not isinstance(bucket, list):
                continue
            if limit is None:
                bucket.extend(value)
                continue
            for entry in value:
                if len(bucket) >= limit:
                    break
                if entry not in bucket:
                    bucket.append(entry)
            continue
        if isinstance(value, str):
            if value and not str(aggregate.get(key) or ""):
                aggregate[key] = value
            continue
        if isinstance(value, dict):
            aggregate.setdefault(key, value)
            continue
        aggregate.setdefault(key, value)
    return aggregate


def changed_target_dirs(strm_changes: list[dict[str, Any]] | None) -> list[str]:
    """从 STRM 变化清单提取去重后的目标相对目录，供增量同步与刷新使用。"""
    seen: list[str] = []
    known: set[str] = set()
    for change in strm_changes or []:
        if not isinstance(change, dict):
            continue
        rel_dir = str(change.get("rel_dir") or "").strip().strip("/")
        if not rel_dir or rel_dir in known:
            continue
        known.add(rel_dir)
        seen.append(rel_dir)
    return seen


def changed_strm_paths(strm_changes: list[dict[str, Any]] | None) -> list[str]:
    """从 STRM 变化清单提取去重后的目标相对文件路径。"""
    seen: list[str] = []
    known: set[str] = set()
    for change in strm_changes or []:
        if not isinstance(change, dict):
            continue
        name = str(change.get("name") or "").strip()
        if not name:
            continue
        rel_dir = str(change.get("rel_dir") or "").strip().strip("/")
        path = f"{rel_dir}/{name}" if rel_dir else name
        if path in known:
            continue
        known.add(path)
        seen.append(path)
    return seen


def build_group_result(
    task: OrganizeGroupTask,
    group_stats: dict,
    *,
    status: str = "",
    error: str = "",
    elapsed_seconds: float = 0.0,
) -> OrganizeGroupResult:
    """把一次组级运行的原始统计收敛为结构化结果。"""
    stats = dict(group_stats or {})
    changes = [item for item in (stats.get("strm_changes") or []) if isinstance(item, dict)]
    identities: list[str] = []
    for row in stats.get("media_items") or []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or row.get("name") or "").strip()
        if title and title not in identities:
            identities.append(title)
    cleanup = {
        key: stats[key]
        for key in (
            "empty_dirs_cleaned",
            "empty_dir_cleanup_candidates",
            "empty_dir_cleanup_protected",
            "empty_dir_cleanup_not_empty",
            "empty_dir_cleanup_unavailable",
            "empty_dir_cleanup_failed",
            "empty_dir_cleanup_unsupported",
            "empty_dir_cleanup_skipped",
        )
        if key in stats
    }
    if stats.get("empty_dir_cleanup_reasons"):
        cleanup["reasons"] = list(stats.get("empty_dir_cleanup_reasons") or [])
    result = OrganizeGroupResult(
        task=task,
        status=status or resolve_group_status(stats, error=error),
        stage=GROUP_STAGE_DONE,
        identities=identities,
        scanned=int(stats.get("total", 0) or 0),
        moved=int(stats.get("moved", 0) or 0),
        replaced=int(stats.get("replaced", 0) or 0),
        metadata_moved=int(stats.get("metadata_moved", 0) or 0),
        skipped=int(stats.get("skipped", 0) or 0),
        need_confirm=int(stats.get("need_confirm", 0) or 0),
        failed=int(stats.get("failed", 0) or 0),
        failures=[str(item) for item in (stats.get("skip_reasons") or [])][:10],
        changed_target_dirs=changed_target_dirs(changes),
        changed_strm_paths=changed_strm_paths(changes),
        strm_changes=changes,
        cleanup=cleanup,
        error=str(error or ""),
        elapsed_seconds=float(elapsed_seconds or stats.get("total_elapsed_seconds", 0.0) or 0.0),
        stats=stats,
    )
    return result


def resolve_group_status(stats: dict, *, error: str = "") -> str:
    """组状态只由结构化统计决定，保证 Web/TG 与审计口径一致。"""
    if error:
        return GROUP_STATUS_FAILED
    if int((stats or {}).get("stopped", 0) or 0):
        return GROUP_STATUS_STOPPED
    unsafe = (
        int((stats or {}).get("failed", 0) or 0)
        or (stats or {}).get("scan_errors")
        or int((stats or {}).get("replacement_cleanup_failed", 0) or 0)
        or int((stats or {}).get("audit_failures", 0) or 0)
        or int((stats or {}).get("empty_dir_cleanup_failed", 0) or 0)
    )
    if unsafe:
        return GROUP_STATUS_PARTIAL
    return GROUP_STATUS_COMPLETED


def is_terminal_group_status(status: str) -> bool:
    return str(status or "") in _TERMINAL_GROUP_STATUSES


@dataclass
class GroupProgress:
    """运行期实时进度投影，供 Web 轮询与 TG 聚合消息读取。"""

    total: int = 0
    completed: int = 0
    current_index: int = 0
    current_group: str = ""
    current_stage: str = GROUP_STAGE_PENDING
    current_file_index: int = 0
    current_file_total: int = 0
    started_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": int(self.total),
            "completed": int(self.completed),
            "current_index": int(self.current_index),
            "current_group": self.current_group,
            "current_stage": self.current_stage,
            "current_stage_label": GROUP_STAGE_LABELS.get(self.current_stage, ""),
            "current_file_index": int(self.current_file_index),
            "current_file_total": int(self.current_file_total),
            "elapsed_seconds": (
                round(max(0.0, time.monotonic() - self.started_at), 3)
                if self.started_at
                else 0.0
            ),
        }
