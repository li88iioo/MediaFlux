"""统一 Telegram Bot 客户端与安全通知模板。

通知发送和命令 polling 共用同一个延迟初始化的 ``TeleBot`` 实例。未配置
Token 或会话 ID 时静默降级为日志；配置热更新由 ``app.bot.restart_bot``
先停止旧 polling，再重置这里的客户端。
"""
from __future__ import annotations

import html
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from app.config import get
from app.logger import configure_telebot_logging, get_logger

logger = get_logger(__name__)

_bot = None
_chat_id: Optional[str] = None
_init_lock = threading.Lock()
_initialized = False

_MESSAGE_LIMIT = 4000
_CAPTION_LIMIT = 1000
_HTML_TOKEN_RE = re.compile(r"<[^>]+>|&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]+);")
_TAG_RE = re.compile(r"<\s*(/?)\s*([a-zA-Z0-9]+)(?:\s[^>]*)?>")
_LEADING_EMOJI_RE = re.compile(r"^[\U0001F000-\U0001FAFF\u2600-\u27BF]")

_FIELD_EMOJI = {
    "任务": "🆔",
    "触发": "🚀",
    "触发方式": "🚀",
    "来源": "☁️",
    "范围": "📂",
    "源目录": "📂",
    "目录": "📂",
    "路径": "📂",
    "分类": "🗂️",
    "视频": "🎞️",
    "总视频": "🎞️",
    "媒体文件": "🎞️",
    "文件": "📄",
    "版本": "🎛️",
    "体积": "💾",
    "本次": "📺",
    "本季": "📊",
    "缺集": "🧩",
    "说明": "ℹ️",
    "已移动": "📥",
    "生成": "🧩",
    "STRM 变化": "🧩",
    "元数据": "📎",
    "跳过": "⏭️",
    "需确认": "⚠️",
    "待确认原因": "⚠️",
    "失败": "❌",
    "错误": "❌",
    "原因": "⚠️",
    "清理": "🧹",
    "媒体库刷新": "🔄",
    "耗时": "⏱️",
    "总耗时": "⏱️",
    "TMDB": "🎬",
    "qBittorrent": "⬇️",
    "光鸭云盘": "☁️",
}


def _has_leading_emoji(value: object) -> bool:
    return bool(_LEADING_EMOJI_RE.match(str(value or "").lstrip()))


def decorate_title(title: object) -> str:
    text = str(title or "").strip()
    if not text or _has_leading_emoji(text):
        return text
    lowered = text.lower()
    if any(word in text for word in ("失败", "错误", "异常")):
        emoji = "❌"
    elif any(word in text for word in ("待确认", "需确认", "部分", "未启动", "警告")):
        emoji = "⚠️"
    elif any(word in text for word in ("完成", "成功")):
        emoji = "✅"
    elif any(word in text for word in ("启动", "开始", "已提交")):
        emoji = "🚀"
    elif any(word in text for word in ("新片", "电影")):
        emoji = "🎬"
    elif "剧集" in text:
        emoji = "📺"
    elif "下载" in text:
        emoji = "⬇️"
    elif "strm" in lowered:
        emoji = "🧩"
    elif "光鸭" in text:
        emoji = "☁️"
    else:
        emoji = "ℹ️"
    return f"{emoji} {text}"


def decorate_field_label(label: object) -> str:
    text = str(label or "").strip()
    if not text or _has_leading_emoji(text):
        return text
    emoji = _FIELD_EMOJI.get(text)
    if not emoji:
        for keyword, candidate in _FIELD_EMOJI.items():
            if keyword in text:
                emoji = candidate
                break
    return f"{emoji} {text}" if emoji else text


@dataclass(frozen=True)
class NotificationAction:
    """Telegram 内联按钮；仅允许短的 opaque token 或 handler 严格白名单动作码。"""

    label: object
    callback_data: str


@dataclass(frozen=True)
class NotificationEvent:
    """可安全渲染的结构化 Telegram 通知。"""

    title: object
    fields: Sequence[tuple[object, object]] = field(default_factory=tuple)
    lines: Sequence[object] = field(default_factory=tuple)
    image_url: str = ""
    footer: object = ""
    actions: Sequence[NotificationAction] = field(default_factory=tuple)
    layout: str = "default"
    field_emojis: bool = True


@dataclass(frozen=True)
class TelegramSendResult:
    """一次文本投递的结构化结果，供持久 Outbox 决定退避窗口。"""

    ok: bool
    retry_after_seconds: int = 0
    error: str = ""
    status_code: int = 0



_TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w780"
_VERSION_PATTERNS = (
    ((r"(?i)(?:2160p|\b4k\b|\buhd\b)"), "2160p"),
    ((r"(?i)1080p"), "1080p"),
    ((r"(?i)720p"), "720p"),
    ((r"(?i)480p"), "480p"),
    ((r"(?i)\bremux\b"), "Remux"),
    ((r"(?i)blu[ ._-]?ray"), "BluRay"),
    ((r"(?i)web[ ._-]?dl"), "WEB-DL"),
    ((r"(?i)web[ ._-]?rip"), "WEBRip"),
    ((r"(?i)\bhdtv\b"), "HDTV"),
    ((r"(?i)(?:\bdovi\b|dolby[ ._-]?vision)"), "DoVi"),
    ((r"(?i)hdr10\+|hdr10plus"), "HDR10+"),
    ((r"(?i)\bhdr10\b"), "HDR10"),
    ((r"(?i)\bhdr\b"), "HDR"),
)


def _item_value(item: object, key: str, default=None):
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _first_value(items: Sequence[object], key: str, default=""):
    for item in items:
        value = _item_value(item, key, default)
        if value not in (None, ""):
            return value
    return default


def safe_int(value: object, default=0, *, minimum: int | None = None):
    """把外部动态数值安全转换为整数，失败时返回默认值。"""
    try:
        result = int(float(value))
    except (TypeError, ValueError, OverflowError):
        result = default
    if minimum is not None and result is not None:
        result = max(minimum, result)
    return result


def _format_size(size: int | float) -> str:
    value = float(safe_int(size, 0, minimum=0))
    units = ("B", "KB", "MB", "GB", "TB")
    unit = units[0]
    for candidate in units:
        unit = candidate
        if value < 1024 or candidate == units[-1]:
            break
        value /= 1024
    return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"


def _format_category(value: object) -> str:
    if isinstance(value, (list, tuple)):
        parts = [str(part).strip() for part in value if str(part).strip()]
    else:
        parts = [part.strip() for part in re.split(r"\s*/\s*", str(value or "")) if part.strip()]
    return " / ".join(parts)


def _format_version(*values: object) -> str:
    source = " ".join(str(value or "") for value in values)
    found = []
    for pattern, label in _VERSION_PATTERNS:
        if re.search(pattern, source) and label not in found:
            found.append(label)
    return " · ".join(found)


def _format_episode_range(episodes: Sequence[int]) -> str:
    values = sorted({
        parsed for value in episodes
        if (parsed := safe_int(value, 0, minimum=0)) > 0
    })
    if not values:
        return ""
    ranges: list[str] = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append(f"E{start:02d}-E{previous:02d}" if start != previous else f"E{start:02d}")
        start = previous = value
    ranges.append(f"E{start:02d}-E{previous:02d}" if start != previous else f"E{start:02d}")
    return "、".join(ranges)


def _format_season_episode_range(season: int | None, episodes: Sequence[int]) -> str:
    values = sorted({
        parsed for value in episodes
        if (parsed := safe_int(value, 0, minimum=0)) > 0
    })
    if not values:
        return ""
    prefix = f"S{season:02d}" if season is not None else ""
    ranges: list[str] = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        start_label = f"{prefix}E{start:02d}"
        end_label = f"{prefix}E{previous:02d}"
        ranges.append(start_label if start == previous else f"{start_label}-{end_label}")
        start = previous = value
    start_label = f"{prefix}E{start:02d}"
    end_label = f"{prefix}E{previous:02d}"
    ranges.append(start_label if start == previous else f"{start_label}-{end_label}")
    return "、".join(ranges)


def _tmdb_image_url(items: Sequence[object]) -> str:
    direct = str(_first_value(items, "image_url", "") or "").strip()
    if direct:
        return direct
    path = str(
        _first_value(items, "backdrop_path", "")
        or _first_value(items, "poster_path", "")
        or ""
    ).strip()
    if not path:
        return ""
    if path.startswith(("http://", "https://")):
        return path
    return f"{_TMDB_IMAGE_BASE}/{path.lstrip('/')}"


def build_stats_event(title: object, stats: Mapping[object, object], *,
                      footer: object = "", image_url: str = "") -> NotificationEvent:
    """构造数据不足时使用的结构化统计卡片。"""
    return NotificationEvent(
        title=title,
        fields=tuple((label, value) for label, value in stats.items() if value not in (None, "")),
        image_url=image_url,
        footer=footer,
    )


def build_media_events(
    items: Sequence[object], *, layout: str = "default", inventory_final: bool = True
) -> list[NotificationEvent]:
    """按 TMDB、媒体类型和季号聚合媒体入库卡片。"""
    groups: dict[tuple[str, str, int | None], list[object]] = {}
    for item in items or ():
        title = str(_item_value(item, "title", "") or "").strip()
        tmdb_id = str(_item_value(item, "tmdb_id", "") or "").strip()
        media_type = str(_item_value(item, "media_type", "") or "").strip().lower()
        if not title or not tmdb_id or media_type not in {"movie", "tv"}:
            continue
        raw_season = _item_value(item, "season")
        season = safe_int(raw_season, None) if media_type == "tv" and raw_season not in (None, "") else None
        groups.setdefault((tmdb_id, media_type, season), []).append(item)

    events: list[NotificationEvent] = []
    for (_identity, media_type, season), current in groups.items():
        title = str(_first_value(current, "title", "未识别媒体") or "未识别媒体")
        year = str(_first_value(current, "year", "") or "")
        title_with_year = f"{title} ({year})" if year else title
        if media_type == "tv":
            season_label = f" · S{season:02d}" if season is not None else ""
            event_title = f"📺 剧集入库：{title_with_year}{season_label}"
        else:
            event_title = f"🎬 新片入库：{title_with_year}"

        fields: list[tuple[object, object]] = []
        episodes = []
        for item in current:
            episode = _item_value(item, "episode")
            parsed_episode = safe_int(episode, None)
            if parsed_episode is not None:
                episodes.append(parsed_episode)
        episode_range = _format_episode_range(episodes)
        if media_type == "tv" and episode_range:
            fields.append(("本次", episode_range))
            season_total = max((
                safe_int(_item_value(item, "season_total", 0), 0, minimum=0)
                for item in current
            ), default=0)
            inventory_known = False
            present_episodes: set[int] = set()
            for item in current:
                inventory = _item_value(item, "season_present_episodes", None)
                if not isinstance(inventory, (list, tuple, set)):
                    continue
                inventory_known = True
                present_episodes.update(
                    parsed for value in inventory
                    if (parsed := safe_int(value, 0, minimum=0)) > 0
                )
            if inventory_known:
                if season_total:
                    expected = set(range(1, season_total + 1))
                    present_in_range = present_episodes & expected
                    missing = sorted(expected - present_in_range)
                    complete = not missing
                    progress = f"{len(present_in_range)} / {season_total} 集"
                    if complete:
                        progress += "（全）"
                    fields.append(("本季", progress))
                    if missing and inventory_final:
                        fields.append((
                            "缺集",
                            _format_season_episode_range(season, missing),
                        ))
                        fields.append((
                            "说明",
                            "未在归档季目录检测到，可能尚未下载、尚未播出或需人工确认",
                        ))
                    elif missing:
                        fields.append((
                            "状态",
                            "本次整理仍有跳过、待确认、失败或扫描未完成项，暂不生成最终缺集结论",
                        ))
                else:
                    fields.append(("本季", f"{len(present_episodes)} 集"))

        source = _first_value(current, "source", "")
        category = _format_category(_first_value(current, "category", ""))
        explicit_versions = " ".join(str(_item_value(item, "version", "") or "") for item in current)
        filenames = " ".join(str(_item_value(item, "filename", "") or "") for item in current)
        version = _format_version(explicit_versions, filenames)
        total_size = sum(
            safe_int(_item_value(item, "size", 0), 0, minimum=0)
            for item in current
        )
        tmdb_id = _first_value(current, "tmdb_id", "")
        fields.extend((
            ("来源", source),
            ("分类", category),
            ("版本", version),
        ))
        if layout == "relaxed":
            file_summary = f"{len(current)} 个"
            if total_size > 0:
                file_summary += f" · {_format_size(total_size)}"
            fields.append(("文件", file_summary))
        else:
            fields.extend((
                ("文件", f"{len(current)} 个"),
                ("体积", _format_size(total_size)),
            ))
        fields.append(("TMDB", tmdb_id))
        events.append(NotificationEvent(
            title=event_title,
            fields=tuple(
                (label, value) for label, value in fields if value not in (None, "")
            ),
            image_url=_tmdb_image_url(current),
            layout=layout,
        ))
    return events

def _env_enabled(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _init() -> None:
    global _bot, _chat_id, _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        if _env_enabled("MEDIAFLUX_TEST_MODE") and not _env_enabled(
            "MEDIAFLUX_TEST_ALLOW_TELEGRAM"
        ):
            _bot = None
            _chat_id = None
            _initialized = True
            logger.info("测试模式已禁用 Telegram transport")
            return
        token = get("TG_BOT_TOKEN").strip()
        _chat_id = get("TG_CHAT_ID").strip() or None
        if token:
            try:
                import telebot

                configure_telebot_logging()
                _bot = telebot.TeleBot(token, parse_mode="HTML")
                logger.info("Telegram Bot 客户端已初始化")
            except Exception as exc:
                logger.error(f"Telegram Bot 初始化失败: {exc}")
                _bot = None
        else:
            logger.warning("未配置 TG_BOT_TOKEN，Telegram 功能降级为日志")
        _initialized = True


def get_bot():
    """返回通知和 polling 共用的 Bot 实例；未配置时返回 ``None``。"""
    _init()
    return _bot


def render_event(event: NotificationEvent) -> str:
    """把结构化事件渲染为 Telegram HTML，并转义全部动态内容。"""
    relaxed = str(event.layout or "default") == "relaxed"
    body = [f"<b>{html.escape(decorate_title(event.title))}</b>"]
    content_started = False
    for label, value in event.fields:
        if value in (None, ""):
            continue
        if relaxed and not content_started:
            body.append("")
        if relaxed:
            safe_label = html.escape(str(label or "").strip())
            body.append(f"<b>{safe_label}</b>  {html.escape(str(value))}")
        else:
            label_text = (
                decorate_field_label(label)
                if event.field_emojis
                else str(label or "").strip()
            )
            safe_label = html.escape(label_text)
            body.append(f"<b>{safe_label}：</b>{html.escape(str(value))}")
        content_started = True
    lines = [
        html.escape(str(line)) for line in event.lines if line not in (None, "")
    ]
    if lines:
        if relaxed and (content_started or len(body) == 1):
            body.append("")
        body.extend(lines)
        content_started = True
    if event.footer not in (None, ""):
        if relaxed and (content_started or len(body) == 1):
            body.append("")
        body.append(html.escape(str(event.footer)))
    return "\n".join(body)


def format_event(title: str, *lines: object) -> str:
    """兼容旧调用的统一 HTML 事件消息。"""
    return render_event(NotificationEvent(title=title, lines=lines))


def _tag_transition(stack: list[tuple[str, str]], token: str) -> list[tuple[str, str]]:
    """返回消费一个 HTML 标签后的开放标签栈。"""
    match = _TAG_RE.fullmatch(token)
    if not match or token.rstrip().endswith("/>"):
        return list(stack)
    closing, name = match.groups()
    name = name.lower()
    result = list(stack)
    if closing:
        for index in range(len(result) - 1, -1, -1):
            if result[index][0] == name:
                del result[index:]
                break
    else:
        result.append((name, token))
    return result


def _close_tags(stack: Sequence[tuple[str, str]]) -> str:
    return "".join(f"</{name}>" for name, _opening in reversed(stack))


def _open_tags(stack: Sequence[tuple[str, str]]) -> str:
    return "".join(opening for _name, opening in stack)


def _split_long_line(line: str, limit: int) -> list[str]:
    """拆分超长单行，保证 entity 和已识别 HTML 标签不被截断。"""
    tokens: list[str] = []
    cursor = 0
    for match in _HTML_TOKEN_RE.finditer(line):
        tokens.extend(line[cursor:match.start()])
        tokens.append(match.group(0))
        cursor = match.end()
    tokens.extend(line[cursor:])

    chunks: list[str] = []
    stack: list[tuple[str, str]] = []
    current = ""
    prefix_length = 0
    for token in tokens:
        next_stack = _tag_transition(stack, token) if token.startswith("<") else list(stack)
        suffix = _close_tags(next_stack)
        if current and len(current) + len(token) + len(suffix) > limit and len(current) > prefix_length:
            current += _close_tags(stack)
            chunks.append(current)
            current = _open_tags(stack)
            prefix_length = len(current)
        # 极端超长纯文本 token 不存在（普通文本逐字符拆分）；超长标签保持原子性。
        current += token
        stack = next_stack
    if current:
        current += _close_tags(stack)
        chunks.append(current)
    return chunks or [""]


def _has_oversized_html_tag(text: str, limit: int) -> bool:
    """判断 HTML 标签本身是否无法在限制内保持闭合。"""
    for match in _HTML_TOKEN_RE.finditer(text):
        token = match.group(0)
        if not token.startswith("<"):
            continue
        tag = _TAG_RE.fullmatch(token)
        closing_overhead = 0
        if tag and not tag.group(1) and not token.rstrip().endswith("/>"):
            closing_overhead = len(tag.group(2)) + 3
        if len(token) + closing_overhead > limit:
            return True
    return False


def split_message(text: str, limit: int = _MESSAGE_LIMIT) -> list[str]:
    """优先按自然行分段，超长单行按 HTML 安全边界继续拆分。"""
    if limit <= 0:
        raise ValueError("limit 必须大于 0")
    text = str(text or "")
    if _has_oversized_html_tag(text, limit):
        text = html.escape(text)
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        pieces = [line] if len(line) <= limit else _split_long_line(line, limit)
        for piece in pieces:
            candidate = f"{current}\n{piece}" if current else piece
            if current and len(candidate) > limit:
                chunks.append(current)
                current = piece
            else:
                current = candidate
    if current or not chunks:
        chunks.append(current)
    return chunks


def _event_markup(event: NotificationEvent):
    actions = tuple(event.actions or ())
    if not actions:
        return None
    try:
        from telebot import types

        markup = types.InlineKeyboardMarkup(row_width=1)
        for action in actions:
            label = str(action.label or "").strip()[:64]
            callback_data = str(action.callback_data or "").strip()
            if (
                not label
                or not callback_data
                or len(callback_data.encode("utf-8")) > 64
            ):
                logger.warning("忽略无效 Telegram 通知操作按钮")
                continue
            markup.add(types.InlineKeyboardButton(
                text=label, callback_data=callback_data,
            ))
        return markup if getattr(markup, "keyboard", None) else None
    except Exception as exc:
        logger.warning("Telegram 操作按钮构造失败 type=%s", type(exc).__name__)
        return None


def _send_text(bot, target: str, text: str, *, reply_markup=None) -> bool:
    chunks = split_message(text)
    for index, chunk in enumerate(chunks):
        kwargs = {"reply_markup": reply_markup} if reply_markup is not None and index == len(chunks) - 1 else {}
        bot.send_message(target, chunk, **kwargs)
    return True


def _telegram_send_error(exc: Exception) -> TelegramSendResult:
    """提取 Telegram 错误码与 retry_after，并避免把 Bot Token 写入日志。"""
    result_json = getattr(exc, "result_json", None)
    payload = result_json if isinstance(result_json, Mapping) else {}
    parameters = payload.get("parameters")
    parameters = parameters if isinstance(parameters, Mapping) else {}

    def positive_int(value: object) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    status_code = positive_int(
        payload.get("error_code") or getattr(exc, "error_code", 0)
    )
    retry_after = positive_int(
        parameters.get("retry_after") or getattr(exc, "retry_after", 0)
    )
    description = str(payload.get("description") or "").strip()
    if not description:
        description = type(exc).__name__
    description = re.sub(
        r"/bot\d+:[^/\s]+", "/bot***", description, flags=re.IGNORECASE,
    )[:300]
    return TelegramSendResult(
        ok=False,
        retry_after_seconds=retry_after,
        error=description,
        status_code=status_code,
    )


def send_result(text: str, chat_id: Optional[str] = None) -> TelegramSendResult:
    """发送兼容 HTML 文本，并保留 429 等可重试信息。"""
    bot = get_bot()
    target = str(chat_id or _chat_id or "").strip()
    if not bot or not target:
        logger.info(f"[通知降级] {text}")
        return TelegramSendResult(ok=False, error="Telegram Bot 或 Chat ID 未配置")
    try:
        return TelegramSendResult(ok=bool(_send_text(bot, target, text)))
    except Exception as exc:
        result = _telegram_send_error(exc)
        logger.error(
            "Telegram 发送失败 type=%s status=%s retry_after=%s error=%s",
            type(exc).__name__, result.status_code or "-",
            result.retry_after_seconds or "-", result.error,
        )
        return result


def send(text: str, chat_id: Optional[str] = None) -> bool:
    """兼容旧调用方的布尔发送接口。"""
    return send_result(text, chat_id=chat_id).ok


def _telegram_message_is_unchanged(exc: Exception) -> bool:
    """Telegram 已是目标内容时，视为幂等编辑成功。"""
    descriptions = [str(exc or "")]
    result_json = getattr(exc, "result_json", None)
    if isinstance(result_json, Mapping):
        descriptions.append(str(result_json.get("description") or ""))
    return any(
        "message is not modified" in description.casefold()
        for description in descriptions
    )


def edit_event(
    event: NotificationEvent, *, chat_id: str, message_id: int | str
) -> bool:
    """原位更新一条结构化 Telegram 消息；失败时由调用方决定是否另发回执。"""
    bot = get_bot()
    target = str(chat_id or "").strip()
    try:
        resolved_message_id = int(message_id)
    except (TypeError, ValueError):
        return False
    if not bot or not target or resolved_message_id <= 0:
        return False
    text = render_event(event)
    if len(split_message(text)) != 1:
        logger.warning("Telegram 原位更新内容过长，改用新消息回执")
        return False
    try:
        bot.edit_message_text(
            text, target, resolved_message_id, reply_markup=_event_markup(event)
        )
        return True
    except Exception as exc:
        if _telegram_message_is_unchanged(exc):
            logger.info("Telegram 原位消息已是目标内容，按幂等成功处理")
            return True
        logger.warning("Telegram 原位更新失败 type=%s", type(exc).__name__)
        return False


def send_event(event: NotificationEvent, chat_id: Optional[str] = None) -> bool:
    """发送结构化事件；图片失败时自动回退完整文本通知。"""
    bot = get_bot()
    target = str(chat_id or _chat_id or "").strip()
    text = render_event(event)
    if not bot or not target:
        logger.info(f"[通知降级] {text}")
        return False
    image_url = str(event.image_url or "").strip()
    reply_markup = _event_markup(event)
    if not image_url:
        try:
            return _send_text(bot, target, text, reply_markup=reply_markup)
        except Exception as exc:
            logger.error(f"Telegram 发送失败: {exc}")
            return False

    caption_chunks = split_message(text, _CAPTION_LIMIT)
    try:
        photo_kwargs = {"reply_markup": reply_markup} if reply_markup is not None and len(caption_chunks) == 1 else {}
        bot.send_photo(target, image_url, caption=caption_chunks[0], **photo_kwargs)
    except Exception as exc:
        logger.warning(f"Telegram 图片发送失败，回退文本: {exc}")
        try:
            return _send_text(bot, target, text, reply_markup=reply_markup)
        except Exception as fallback_exc:
            logger.error(f"Telegram 文本回退失败: {fallback_exc}")
            return False
    try:
        for index, chunk in enumerate(caption_chunks[1:]):
            kwargs = {"reply_markup": reply_markup} if reply_markup is not None and index == len(caption_chunks) - 2 else {}
            bot.send_message(target, chunk, **kwargs)
        return True
    except Exception as exc:
        logger.error(f"Telegram 图片通知续发失败: {exc}")
        return False


def notify_gcid_import_started(*, task_id: int, file_count: int, total_size: int,
                               retry: bool = False) -> None:
    """发送 GCID 导入开始统计，不包含清单、GCID、凭据或私有响应。"""
    send_event(NotificationEvent(
        "GCID 重试开始" if retry else "GCID 导入开始",
        fields=(
            ("任务", f"#{int(task_id)}"),
            ("文件", f"{max(0, int(file_count))} 个"),
            ("体积", _format_size(max(0, int(total_size)))),
        ),
    ))


def notify_gcid_import_finished(*, task_id: int, status: str, success_count: int,
                                failed_count: int, failed_samples: Sequence[object] = (),
                                retry: bool = False) -> None:
    """发送安全的 GCID 导入结果；失败样本最多三条且只含路径和通用错误。"""
    normalized = str(status or "failed")
    if normalized == "success":
        title = "GCID 重试成功" if retry else "GCID 导入成功"
    elif normalized == "partial_success":
        title = "GCID 重试部分成功" if retry else "GCID 导入部分成功"
    else:
        title = "GCID 重试失败" if retry else "GCID 导入失败"
    lines = []
    for sample in tuple(failed_samples or ())[:3]:
        path = str(_item_value(sample, "path", "") or "").strip()[:240]
        if path:
            lines.append(f"{path}：导入失败")
    send_event(NotificationEvent(
        title,
        fields=(
            ("任务", f"#{int(task_id)}"),
            ("成功", f"{max(0, int(success_count))} 个"),
            ("失败", f"{max(0, int(failed_count))} 个"),
        ),
        lines=tuple(lines),
    ))


def notify_organize_done(summary: str) -> None:
    send_event(NotificationEvent("光鸭整理完成", lines=(summary,)))


def notify_strm_done(summary: str) -> None:
    send_event(NotificationEvent("STRM 同步完成", lines=(summary,)))


def notify_download(source: str, title: str, path: str) -> None:
    send_event(NotificationEvent(
        "下载完成",
        fields=(("来源", source), ("任务", title), ("路径", path)),
    ))


def reset() -> None:
    """清空共享客户端；调用前必须先停止旧 polling。"""
    global _bot, _chat_id, _initialized
    with _init_lock:
        _bot = None
        _chat_id = None
        _initialized = False
