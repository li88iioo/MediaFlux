"""STRM 文件变化通知格式化。

仅保存相对路径与错误摘要，避免把本地根目录、签名 URL 或令牌写入通知。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from app.logger import redact_sensitive_text

_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_OVERWRITE_CONFLICT = "覆盖 STRM：已停止，本地文件内容与 STRM 索引不一致"
_CONFLICT_SUBJECT = re.compile(r"(?:(.+)/)?(?:生成 STRM|精准增量) (.+)")
_CONFLICT_NOTICE = "本地 STRM 内容与索引不一致，已保留原文件。"
_EPISODE_NAME = re.compile(
    r"^(.+?)[. _]+((?:19|20)\d{2})[. _]+(S\d+E\d+(?:(?:[-~]E?|E)\d+)*)(?=[. _-]|$)", re.IGNORECASE
)

ROUND_CLEANUP_BLOCKED_REASON = (
    "整轮扫描未完整或存在异常，已跳过删除型清理、来源退役与空目录清理"
)


def sync_notice_lines(stats: Mapping[str, object]) -> tuple[str, ...]:
    """从有界错误样本生成通知，完整错误保留在运行记录和失败台账。"""
    conflicts: list[tuple[str, str, str]] = []
    other: list[str] = []
    samples = stats.get("error_samples")
    for sample in samples if isinstance(samples, (list, tuple)) else ():
        text = _safe_text(sample, limit=600)
        if not text or text.rsplit("/", 1)[-1] == ROUND_CLEANUP_BLOCKED_REASON:
            continue
        if len(conflicts) + len(other) == 3:
            break
        subject, marker, _ = text.partition("：" + _OVERWRITE_CONFLICT)
        if marker and (match := _CONFLICT_SUBJECT.fullmatch(subject)):
            source, filename = match[1] or "", match[2]
            episode = _EPISODE_NAME.match(filename)
            label = f"{episode[1]} ({episode[2]}) · {episode[3].upper()}" if episode else filename
            conflicts.append((source, label, filename))
        else:
            other.append(text if len(text) <= 140 else text[:139] + "…")

    blocks = []
    if conflicts:
        sources = {source for source, _, _ in conflicts}
        heading = "⚠️ 需核对的文件"
        if len(sources) == 1 and conflicts[0][0]:
            heading += f" · {conflicts[0][0]}"
        keys = [(source, label) for source, label, _ in conflicts]
        items = [heading]
        for source, label, filename in conflicts:
            name = filename if keys.count((source, label)) > 1 else label
            items.append(f"• {source} · {name}" if source and len(sources) > 1 else f"• {name}")
        items.extend((_CONFLICT_NOTICE, "详情见「STRM 同步 → 索引诊断与台账 → 失败台账」。"))
        blocks.append("\n".join(items))
    if other:
        blocks.append("\n".join(("异常摘要", *("• " + text for text in other))))
    if stats.get("clean_skipped"):
        if any(stats.get(key) for key in ("cleaned", "metadata_cleaned", "empty_dirs_cleaned")):
            blocks.append("🧹 为避免误删，部分待清理项目已保留。")
        else:
            blocks.append("🧹 为避免误删，已暂停本轮删除型清理（失效 STRM、来源退役及空目录）。")
        if stats.get("organize_followup_queued"):
            blocks.append("另有整理联动已排队。")
    return tuple(block if index == 0 else "\n" + block for index, block in enumerate(blocks))


def _safe_text(value: object, *, limit: int, default: str = "") -> str:
    text = redact_sensitive_text(value)
    text = _URL_RE.sub("[URL 已隐藏]", text)
    return " ".join(text.split())[:limit] or default


def _safe_error(value: object) -> str:
    return _safe_text(value, limit=500)


def _safe_label(value: object, default: str) -> str:
    return _safe_text(value, limit=240, default=default)


_ACTIONS = {
    "generated": "🔗",
    "metadata": "⬇️",
    "removed": "🧹",
    "removed_dir": "📁",
    "failed": "❌",
}


@dataclass(frozen=True)
class StrmChange:
    """一项可展示的 STRM/元数据变化。"""

    action: str
    directory: str
    filename: str
    error: str = ""

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def relative_change(
    action: str,
    target: str | Path,
    strm_root: str | Path,
    *,
    error: object = "",
) -> StrmChange:
    """将目标文件转换为相对于 ``光鸭云盘`` 根的安全变化记录。"""
    target_path = Path(target)
    display_root = Path(strm_root) / "光鸭云盘"
    try:
        relative = target_path.relative_to(display_root)
    except (ValueError, TypeError):
        # 路径异常时仅保留文件名，绝不回退展示绝对路径。
        relative = Path(target_path.name)
    directory = relative.parent.as_posix()
    if directory == ".":
        directory = "根目录"
    return StrmChange(
        action=action if action in _ACTIONS else "failed",
        directory=_safe_label(directory, "根目录"),
        filename=_safe_label(relative.name, "未知文件"),
        error=_safe_error(error),
    )


def append_change(stats: dict, change: StrmChange | Mapping[str, object], *, limit: int = 5000) -> None:
    """向统计结果追加有界变化记录，超限只累计省略数量。"""
    changes = stats.setdefault("changes", [])
    if len(changes) >= max(0, limit):
        stats["omitted_count"] = int(stats.get("omitted_count", 0) or 0) + 1
        return
    if isinstance(change, StrmChange):
        changes.append(change.as_dict())
    else:
        changes.append({
            "action": str(change.get("action") or "failed"),
            "directory": _safe_label(change.get("directory"), "根目录"),
            "filename": _safe_label(change.get("filename"), "未知文件"),
            "error": _safe_error(change.get("error")),
        })


def _normalize(changes: Iterable[StrmChange | Mapping[str, object]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for item in changes:
        raw = item.as_dict() if isinstance(item, StrmChange) else item
        action = str(raw.get("action") or "failed")
        normalized.append({
            "action": action if action in _ACTIONS else "failed",
            "directory": _safe_label(raw.get("directory"), "根目录").strip("/") or "根目录",
            "filename": _safe_label(Path(str(raw.get("filename") or "未知文件")).name, "未知文件"),
            "error": _safe_error(raw.get("error")),
        })
    return normalized


def _render_page(
    page_items: Sequence[dict[str, str]],
    *,
    page: int,
    pages: int,
    start: int,
    total: int,
    source_tag: str,
) -> str:
    end = start + len(page_items) - 1
    lines = [f"📄 [{source_tag}] STRM 文件明细 {page}/{pages}（{start}-{end}/{total}）", ""]
    grouped: dict[tuple[str, str], list[dict[str, str]]] = {}
    for item in page_items:
        grouped.setdefault((item["action"], item["directory"]), []).append(item)

    group_items = list(grouped.items())
    for group_index, ((action, directory), files) in enumerate(group_items):
        lines.append(f"--- {_ACTIONS[action]} {directory}")
        for file_index, item in enumerate(files):
            branch = "└──" if file_index == len(files) - 1 else "├──"
            error = _CONFLICT_NOTICE if item["error"].startswith(_OVERWRITE_CONFLICT) else item["error"][:180]
            suffix = f"（{error}）" if error else ""
            lines.append(f"      {branch} {item['filename']}{suffix}")
        if group_index != len(group_items) - 1:
            lines.append("")
    return "\n".join(lines)


def _chunk_by_length(
    items: list[dict[str, str]],
    *,
    max_items: int,
    max_length: int,
    source_tag: str,
) -> list[list[dict[str, str]]]:
    chunks: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    for item in items:
        candidate = [*current, item]
        preview = _render_page(
            candidate,
            page=1,
            pages=1,
            start=1,
            total=len(items),
            source_tag=source_tag,
        )
        if current and (len(candidate) > max_items or len(preview) > max_length):
            chunks.append(current)
            current = [item]
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def build_strm_detail_messages(
    changes: Iterable[StrmChange | Mapping[str, object]],
    *,
    source_tag: str = "光鸭整理触发",
    max_items: int = 20,
    max_length: int = 2600,
    omitted_count: int = 0,
    max_messages: int = 0,
) -> list[str]:
    """生成 TgtoDrive 风格、按文件数与字符数双重分页的通知。

    ``max_messages`` 为正数时，预计消息条数超过上限只发一条摘要，避免
    大库同步把 Telegram 刷爆；摘要必须明确说明明细未发送和查看入口。
    """
    items = _normalize(changes)
    if not items:
        return []
    max_items = max(1, (max_items or 20))
    max_length = max(400, (max_length or 2600))
    chunks = _chunk_by_length(
        items,
        max_items=max_items,
        max_length=max_length,
        source_tag=source_tag,
    )
    total = len(items)
    pages = len(chunks)
    limit = max(0, (max_messages or 0))
    if limit and pages > limit:
        omitted = (omitted_count or 0)
        summary = (
            f"📄 {source_tag} · STRM 变化明细\n"
            f"本轮共 {total} 条变化，预计需要 {pages} 条消息，已超过上限 {limit} 条。\n"
            "为避免刷屏，本次只发送摘要；完整明细请在 Web 运行记录中查看。"
        )
        if omitted:
            summary += f"\n另有 {omitted} 条变化因数量上限未记录。"
        return [summary[:max_length]]
    messages: list[str] = []
    start = 1
    for page, chunk in enumerate(chunks, start=1):
        message = _render_page(
            chunk,
            page=page,
            pages=pages,
            start=start,
            total=total,
            source_tag=source_tag,
        )
        if page == pages and omitted_count:
            message += f"\n\n⚠️ 另有 {omitted_count} 条变化因数量上限未展示"
        messages.append(message[:max_length])
        start += len(chunk)
    return messages
