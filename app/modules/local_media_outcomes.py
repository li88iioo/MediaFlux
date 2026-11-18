"""本地整理文件事实投影；任务结束并不等于本次归档了视频。"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

LOCAL_PREVIEW_WARNING = "仅预览模式：未移动文件"
LOCAL_CONFLICT_SKIP_PREFIX = "按冲突策略保留现有文件，跳过 "
LOCAL_REFRESH_NO_TARGET = "媒体库刷新未入队：无可用刷新目标"
QB_CLEANUP_WARNING = "qB 任务移除失败，任务保持暂停。请在 qB 中手动移除任务并保留文件"
LOCAL_REFRESH_LABELS = {
    "completed": "刷新完成 🎯",
    "queued": "已排队（合并刷新） ⏳",
    "failed": "刷新失败（需处理） ❌",
    "skipped": "未入队",
}


def _field(value: Any, name: str, default: Any = "") -> Any:
    try:
        return value[name]
    except (KeyError, IndexError, TypeError):
        return getattr(value, name, default)


def _basename(value: Any) -> str:
    # 本地任务也可能来自 Windows 下载器；不把源目录或控制字符带到通知。
    text = str(value or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return " ".join(text.split())[:255]


def local_media_task_outcome(task: Any, items: Iterable[Any]) -> dict[str, Any]:
    """共享给通知和 Agent 的有限文件事实，不含 ID、路径、哈希或原始错误。"""
    videos = [item for item in items if _field(item, "role") == "video"]
    names = list(
        dict.fromkeys(_basename(_field(item, "source_path")) for item in videos)
    )
    status = str(_field(task, "status"))
    preview_only = LOCAL_PREVIEW_WARNING in str(_field(task, "warning"))
    completed = status == "completed" and not preview_only
    archived = (
        sum(_field(item, "action") in {"move", "replace"} for item in videos)
        if completed
        else 0
    )
    skipped = (
        sum(_field(item, "action") == "skip" for item in videos) if completed else 0
    )
    unknown = len(videos) - archived - skipped
    if preview_only:
        outcome = "preview_only"
    elif status != "completed":
        outcome = "pending" if status not in {"failed", "cancelled"} else status
    elif not videos or unknown:
        outcome = "unknown" if not archived and not skipped else "partial"
    elif skipped == len(videos):
        outcome = "conflict_skipped"
    elif archived == len(videos):
        outcome = "archived"
    else:
        outcome = "partial"
    return {
        "original_filename": _basename(_field(task, "content_path")),
        "file_names": [name for name in names[:20] if name],
        "file_names_truncated": len(names) > 20,
        "file_outcome": outcome,
        "video_count": len(videos),
        "archived_video_count": archived,
        "skipped_video_count": skipped,
        "unknown_video_count": unknown,
        "created_at": str(_field(task, "created_at")),
        "updated_at": str(_field(task, "updated_at")),
        "completed_at": str(_field(task, "completed_at")),
    }


def local_media_archive_stage(outcome: Mapping[str, Any]) -> tuple[str, str]:
    kind = outcome["file_outcome"]
    archived, skipped = outcome["archived_video_count"], outcome["skipped_video_count"]
    if kind == "preview_only":
        return "skipped", "仅预览，未移动文件"
    if kind == "conflict_skipped":
        return "skipped", f"冲突跳过 {skipped} 项，未新增归档"
    if kind not in {"archived", "partial"} or outcome["unknown_video_count"]:
        return "manual_review", "归档结果待核对"
    detail = f"已归档 {archived} 项" + (f" · 冲突跳过 {skipped} 项" if skipped else "")
    return "completed", detail


def _persisted_warnings(task: Any) -> tuple[str, ...]:
    return tuple(str(_field(task, "warning") or "").split("；"))


def local_media_warning_count(task: Any, result: Any = None) -> int:
    """仅统计需要查看的警告。策略说明仍保留在任务记录里。"""
    warnings = _field(result, "warnings", ()) or _persisted_warnings(task)
    return sum(
        bool(warning)
        and warning not in {LOCAL_PREVIEW_WARNING, LOCAL_REFRESH_NO_TARGET}
        and not re.fullmatch(rf"{LOCAL_CONFLICT_SKIP_PREFIX}\d+ 项", warning)
        for warning in (str(item).strip() for item in warnings)
    )


def local_media_qb_cleanup_pending(task: Any, result: Any = None) -> bool:
    """兼容旧任务的移除失败警告，且保留已经提交的文件归档事实。"""
    return bool(_field(result, "qb_cleanup_pending", False)) or any(
        "qb任务移除失败" in "".join(str(warning).casefold().split())
        for warning in (*_persisted_warnings(task), *(_field(result, "warnings", ()) or ()))
    )


def local_media_refresh_status(task: Any, result: Any = None, *, archive_status: str = "") -> str:
    """投影实际入队回执及任务警告，重建时不把无回执推定为完成。"""
    if archive_status == "skipped":
        return "skipped"
    status = str(_field(result, "media_refresh_status") or "").strip().lower()
    if status in LOCAL_REFRESH_LABELS:
        return status
    warnings = (*_persisted_warnings(task), *(_field(result, "warnings", ()) or ()))
    failure_markers = ("刷新失败", "刷新入队失败", "未刷新媒体库", "已跳过刷新")
    if any(any(marker in str(item) for marker in failure_markers) for item in warnings):
        return "failed"
    if any(str(item).strip() == LOCAL_REFRESH_NO_TARGET for item in warnings):
        return "skipped"
    return ""
