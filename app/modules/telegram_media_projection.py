"""把整理媒体明细投影为可原位更新的 Telegram 文本块。"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from app.notifier import (
    NotificationEvent,
    build_media_events,
    decorate_field_label,
    render_event,
    safe_int,
    telegram_text_length,
)

# Telegram 文本上限为 4096；通知中心按 4000 分段。生命周期消息还会补写
# STRM、媒体库和复核终态，因此提前保留一小段增长空间，确保仍可原位编辑。
_EDIT_SAFE_LIMIT = 3800
_DETAIL_SEPARATOR = "---"
_MEDIA_TITLE_PREFIXES = (
    ("📺 剧集入库：", "📺"),
    ("🎬 新片入库：", "🎬"),
    ("剧集入库：", "📺"),
    ("新片入库：", "🎬"),
)
_MEDIA_FIELD_LABELS = {
    "本次": "📺 本次更新",
    "本季": "📊 本季进度",
    "状态": "⚠️ 整理状态",
    "缺集": "🧩 缺集情况",
    "说明": "ℹ️ 说明",
    "来源": "☁️ 存储来源",
    "分类": "🗂️ 目录分类",
    "版本": "🎛️ 规格版本",
    "文件": "📄 文件统计",
    "体积": "💾 文件体积",
    "TMDB": "🎬 TMDB ID",
}


def _value(item: object, key: str, default: object = "") -> object:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _identified(item: object) -> bool:
    return bool(
        str(_value(item, "title", "") or "").strip()
        and str(_value(item, "tmdb_id", "") or "").strip()
        and str(_value(item, "media_type", "") or "").strip().lower()
        in {"movie", "tv"}
    )


def _episode_range(values: set[int]) -> str:
    ordered = sorted(value for value in values if value > 0)
    if not ordered:
        return ""
    ranges: list[tuple[int, int]] = []
    start = previous = ordered[0]
    for value in ordered[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append((start, previous))
        start = previous = value
    ranges.append((start, previous))
    return ", ".join(
        f"E{start:02d}" if start == end else f"E{start:02d}-E{end:02d}"
        for start, end in ranges
    )


def _compact_blocks(items: Sequence[object]) -> tuple[str, ...]:
    grouped: dict[tuple[str, str, str, int], set[int]] = {}
    for item in items:
        title = str(_value(item, "title", "") or "未识别媒体").strip()
        year = str(_value(item, "year", "") or "").strip()
        media_type = str(_value(item, "media_type", "") or "").strip().lower()
        season = safe_int(_value(item, "season", 0), 0, minimum=0)
        episode = safe_int(_value(item, "episode", 0), 0, minimum=0)
        grouped.setdefault((title, year, media_type, season), set())
        if episode:
            grouped[(title, year, media_type, season)].add(episode)

    blocks: list[str] = []
    for (title, year, media_type, season), episodes in grouped.items():
        suffix = f" ({year})" if year else ""
        if media_type == "tv" and season:
            suffix += f" · S{season:02d}"
            episode_label = _episode_range(episodes)
            if len(episodes) == 1:
                suffix += episode_label
            elif episode_label:
                suffix += f" · {episode_label}"
        marker = "📺" if media_type == "tv" else "🎬"
        blocks.append(f"{marker} {title}{suffix}")
    return tuple(blocks)


def _compact_media_title(value: object) -> str:
    title = str(value or "媒体入库").strip()
    for prefix, marker in _MEDIA_TITLE_PREFIXES:
        if title.startswith(prefix):
            return f"{marker} {title[len(prefix):].strip()}"
    return title


def _media_field_line(label: object, value: object, *, field_emojis: bool) -> str:
    normalized_label = str(label or "").strip()
    display_label = _MEDIA_FIELD_LABELS.get(normalized_label)
    if not display_label:
        display_label = (
            decorate_field_label(normalized_label)
            if field_emojis else normalized_label
        )
    display_value = str(value)
    if (
        normalized_label == "本季"
        and "（全）" in display_value
        and not display_value.rstrip().endswith("✅")
    ):
        display_value = f"{display_value} ✅"
    return f"- {display_label}：{display_value}"


def _event_block(event: NotificationEvent) -> str:
    lines = [_compact_media_title(event.title)]
    for label, value in event.fields:
        if value in (None, ""):
            continue
        lines.append(_media_field_line(
            label, value, field_emojis=event.field_emojis,
        ))
    lines.extend(
        f"- {line!s}" for line in event.lines if line not in (None, "")
    )
    if event.footer not in (None, ""):
        lines.append(f"- ℹ️ {event.footer!s}")
    return "\n".join(lines)


def _separated_blocks(
    blocks: Sequence[str], *, omitted: int = 0,
) -> tuple[str, ...]:
    lines = [f"{_DETAIL_SEPARATOR}\n\n{block}\n" for block in blocks]
    if omitted:
        lines.append(
            f"{_DETAIL_SEPARATOR}\n\n"
            f"…另有 {omitted} 项媒体详情未展开，完整记录见 Web 整理日志\n"
        )
    if lines:
        lines[-1] = f"{lines[-1]}\n{_DETAIL_SEPARATOR}"
    return tuple(lines)


def build_media_detail_blocks(
    items: Sequence[object], *, inventory_final: bool = True,
) -> tuple[str, ...]:
    """恢复旧媒体卡的全部文本字段，并为无法成卡的数据保留紧凑兜底。"""
    rows = tuple(items or ())
    identified = tuple(item for item in rows if _identified(item))
    fallback = tuple(item for item in rows if not _identified(item))
    # 生命周期消息需要持续原位补写 STRM/媒体库状态，因此只复用旧媒体卡的
    # 业务字段，不继承 image_url；否则文本消息与图片 caption 无法稳定使用同一
    # 编辑接口，失败回退还可能额外生成一条重复消息。
    blocks = [
        _event_block(event)
        for event in build_media_events(
            identified, layout="relaxed", inventory_final=inventory_final,
        )
    ]
    blocks.extend(_compact_blocks(fallback))
    return tuple(blocks)


def attach_bounded_media_details(
    event: NotificationEvent, blocks: Sequence[object],
) -> NotificationEvent:
    """把媒体详情装入生命周期消息，超长时明确提示而不破坏原位更新。"""
    normalized = tuple(str(block) for block in blocks if block not in (None, ""))
    if not normalized:
        return event

    full = replace(event, lines=_separated_blocks(normalized))
    if telegram_text_length(render_event(full)) <= _EDIT_SAFE_LIMIT:
        return full

    total = len(normalized)
    best: NotificationEvent | None = None
    low = 0
    high = total - 1
    # 保留块数越少，渲染结果越短。用二分定位最大可保留前缀，避免大批量
    # 整理结果逐项重渲染形成 O(n²) 开销。
    while low <= high:
        keep = (low + high) // 2
        omitted = total - keep
        candidate = replace(
            event,
            lines=_separated_blocks(normalized[:keep], omitted=omitted),
        )
        if telegram_text_length(render_event(candidate)) <= _EDIT_SAFE_LIMIT:
            best = candidate
            low = keep + 1
        else:
            high = keep - 1

    return best or event
