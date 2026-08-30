"""统一 Telegram Bot 客户端与安全通知模板。

通知发送和命令 polling 共用同一个延迟初始化的 ``TeleBot`` 实例。未配置
Token 或会话 ID 时静默降级为日志；配置热更新由 ``app.bot.restart_bot``
先停止旧 polling，再重置这里的客户端。
"""
from __future__ import annotations

import hashlib
import html
import logging
import os
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

import requests

from app.config import get
from app.logger import configure_telebot_logging, get_logger, log_throttled

logger = get_logger(__name__)

_bot = None
_chat_id: Optional[str] = None
_init_lock = threading.Lock()
_initialized = False

_MESSAGE_LIMIT = 4000
_CAPTION_LIMIT = 1000
_HTML_TOKEN_RE = re.compile(r"<[^>]+>|&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]+);")
_TAG_RE = re.compile(r"<\s*(/?)\s*([a-zA-Z0-9]+)(?:\s[^>]*)?>")
_LEADING_EMOJI_RE = re.compile(
    r"^[\U0001F000-\U0001FAFF\u2100-\u214F\u2300-\u23FF\u2600-\u27BF\u2B00-\u2BFF]"
)


def telegram_text_length(value: object) -> int:
    """返回 Telegram 文本边界使用的 UTF-16 code unit 数量。

    Telegram 的消息实体偏移以 UTF-16 code unit 计数。统一使用同一保守
    边界可避免非 BMP emoji 在 Python ``len`` 下被低估，最终被 API 拒绝。
    """
    return sum(2 if ord(char) > 0xFFFF else 1 for char in str(value or ""))


def truncate_telegram_text(value: object, limit: int) -> str:
    """在不拆分 Unicode code point 的前提下截断到 Telegram 文本边界。"""
    maximum = max(0, int(limit or 0))
    text = str(value or "")
    if not text or maximum <= 0:
        return ""
    used = 0
    end = 0
    for end, char in enumerate(text, start=1):
        width = 2 if ord(char) > 0xFFFF else 1
        if used + width > maximum:
            return text[:end - 1]
        used += width
    return text


def _notification_log_title(text: object) -> str:
    """提取适合落盘的短标题，避免降级通知把整段正文写入运行日志。"""
    plain = html.unescape(re.sub(r"<[^>]+>", "", str(text or "")))
    first_line = next((line.strip() for line in plain.splitlines() if line.strip()), "通知")
    return first_line[:120]


def _log_notification_fallback(text: object) -> None:
    title = _notification_log_title(text)
    log_throttled(
        logger,
        logging.INFO,
        f"telegram-notification-fallback:{title}",
        "Telegram 未配置，通知已降级为本地记录 title=%s",
        title,
    )

_FIELD_EMOJI = {
    "任务": "🆔",
    "任务编号": "🆔",
    "队列": "🧾",
    "触发": "🚀",
    "触发方式": "🚀",
    "来源": "☁️",
    "存储来源": "☁️",
    "来源概览": "🧭",
    "同步来源": "🧭",
    "范围": "📂",
    "源目录": "📁",
    "源文件目录": "📁",
    "所在目录": "📁",
    "目录": "📁",
    "路径": "📁",
    "本地目录": "📁",
    "分类": "🗂️",
    "媒体": "🎬",
    "目标媒体": "🎬",
    "候选媒体": "🎬",
    "候选": "🎯",
    "类型": "🎞️",
    "剧集": "📺",
    "置信度": "📈",
    "下载": "⬇️",
    "本地下载": "⬇️",
    "本地归档": "📥",
    "入库复核": "🔍",
    "视频": "🎞️",
    "总视频": "🎞️",
    "媒体文件": "🎞️",
    "文件": "📄",
    "涉及文件": "📄",
    "目标文件": "📄",
    "版本": "🎛️",
    "体积": "💾",
    "本次": "📺",
    "本季": "📊",
    "状态": "📌",
    "处理状态": "📌",
    "执行结果": "📊",
    "结果": "📊",
    "概览": "📋",
    "扫描": "🔎",
    "扫描范围": "🔎",
    "扫描并发": "⚙️",
    "云端请求": "🌐",
    "请求延迟": "⏱️",
    "整理": "📊",
    "人工确认": "🤝",
    "缺集": "🧩",
    "说明": "ℹ️",
    "附带说明": "💡",
    "已移动": "📥",
    "生成": "🧩",
    "STRM 变化": "🧩",
    "STRM 状态": "🔗",
    "STRM": "🔗",
    "元数据": "📎",
    "跳过": "⏭️",
    "需确认": "⚠️",
    "待确认原因": "⚠️",
    "失败": "❌",
    "错误": "❌",
    "错误原因": "⚠️",
    "原因": "⚠️",
    "清理": "🧹",
    "媒体库刷新": "🎯",
    "媒体库": "🎯",
    "订阅": "📡",
    "目标集": "📺",
    "复核结果": "🔍",
    "复核次数": "🔁",
    "已核对剧集": "🔎",
    "缺集剧集": "📺",
    "已播缺集": "🧩",
    "待补": "🧩",
    "待补内容": "🧩",
    "待核对": "⚠️",
    "已提交": "🚀",
    "成功": "✅",
    "目录 ID": "🆔",
    "耗时": "⏱️",
    "阶段耗时": "⏱️",
    "总耗时": "⏱️",
    "TMDB": "🎬",
    "MetaTube": "🎞️",
    "清洗标题": "🧹",
    "qBittorrent": "⬇️",
    "光鸭云盘": "☁️",
}

# ``fields`` 中的空二元组表示卡片分组留白。保持它是普通 tuple，确保事件在
# SQLite/JSON 往返后仍能无损恢复，不需要为纯展示需求扩展持久化协议。
NOTIFICATION_SECTION_BREAK: tuple[str, str] = ("", "")


def _has_leading_emoji(value: object) -> bool:
    text = str(value or "").lstrip()
    if not text:
        return False
    # 兼容 ⏳、⏭、⬇、ℹ 等位于传统 emoji 区段之外的 Unicode 符号，
    # 避免已经带状态图标的标题再次被 decorate_title 加前缀。
    return bool(
        _LEADING_EMOJI_RE.match(text)
        or unicodedata.category(text[0]).startswith("S")
    )


def decorate_title(title: object) -> str:
    text = str(title or "").strip()
    if not text or _has_leading_emoji(text):
        return text
    lowered = text.lower()
    if any(word in text for word in ("失败", "错误", "异常")):
        emoji = "❌"
    elif any(word in text for word in (
        "待确认", "需确认", "需要处理", "需处理", "待处理", "部分",
        "未启动", "未完成", "无法", "警告", "停止", "中断", "取消",
    )):
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
    """一次投递的结构化结果，供持久 Outbox 区分失败与结果未知。"""

    ok: bool
    retry_after_seconds: int = 0
    error: str = ""
    status_code: int = 0
    partially_delivered: bool = False
    message_id: int = 0

    @property
    def outcome_unknown(self) -> bool:
        """网络中断、超时或部分投递后无法安全重放。"""
        return (
            not self.ok
            and (
                self.partially_delivered
                or int(self.status_code or 0) in {0, 408}
            )
        )



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
                logger.error("Telegram Bot 初始化失败 type=%s", type(exc).__name__)
                _bot = None
        else:
            logger.warning("未配置 TG_BOT_TOKEN，Telegram 功能降级为日志")
        _initialized = True


def get_bot():
    """返回通知和 polling 共用的 Bot 实例；未配置时返回 ``None``。"""
    _init()
    return _bot


def notification_target_chat_id(chat_id: Optional[str] = None) -> str:
    """解析主动通知的实际目标会话，不暴露配置读取细节给业务模块。"""
    _init()
    return str(chat_id or _chat_id or "").strip()


def render_event(event: NotificationEvent) -> str:
    """把结构化事件渲染为 Telegram HTML，并转义全部动态内容。"""
    layout = str(event.layout or "default")
    compact_report = layout == "compact_report"
    relaxed = layout in {"relaxed", "compact_report"}
    body = [f"<b>{html.escape(decorate_title(event.title))}</b>"]
    content_started = False
    for label, value in event.fields:
        if relaxed and label in (None, "") and value in (None, ""):
            if content_started and body[-1] != "":
                body.append("")
            continue
        if value in (None, ""):
            continue
        if relaxed and not content_started:
            body.append("")
        if relaxed:
            label_text = (
                decorate_field_label(label)
                if event.field_emojis
                else str(label or "").strip()
            )
            safe_label = html.escape(label_text)
            safe_value = html.escape(str(value))
            value_lines = safe_value.splitlines()
            tree_value = bool(
                compact_report
                and value_lines
                and value_lines[0].lstrip().startswith(("└", "├"))
            )
            if len(value_lines) <= 1 and not tree_value:
                spacer = "" if compact_report else " "
                body.append(f"- <b>{safe_label}：</b>{spacer}{safe_value}")
            else:
                body.append(f"- <b>{safe_label}：</b>")
                if compact_report:
                    body.extend(
                        f"  {line}" if line else "" for line in value_lines
                    )
                else:
                    body.extend(value_lines)
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
            if body[-1] != "":
                body.append("")
        body.extend(lines)
        content_started = True
    if event.footer not in (None, ""):
        if relaxed and (content_started or len(body) == 1):
            if body[-1] != "":
                body.append("")
        footer = str(event.footer)
        if relaxed:
            prefix = "" if _has_leading_emoji(footer) else "ℹ️ "
            safe_footer = f"{prefix}{html.escape(footer)}"
            body.append(
                f"<blockquote>{safe_footer}</blockquote>"
                if compact_report
                else safe_footer
            )
        else:
            body.append(html.escape(footer))
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
        if (
            current
            and telegram_text_length(current + token + suffix) > limit
            and telegram_text_length(current) > prefix_length
        ):
            current += _close_tags(stack)
            chunks.append(current)
            current = _open_tags(stack)
            prefix_length = telegram_text_length(current)
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
        if telegram_text_length(token) + closing_overhead > limit:
            return True
    return False


def split_message(text: str, limit: int = _MESSAGE_LIMIT) -> list[str]:
    """优先按自然行分段，超长单行按 HTML 安全边界继续拆分。"""
    if limit <= 0:
        raise ValueError("limit 必须大于 0")
    text = str(text or "")
    if _has_oversized_html_tag(text, limit):
        text = html.escape(text)
    if telegram_text_length(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        pieces = (
            [line]
            if telegram_text_length(line) <= limit
            else _split_long_line(line, limit)
        )
        for piece in pieces:
            candidate = f"{current}\n{piece}" if current else piece
            if current and telegram_text_length(candidate) > limit:
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
            label = truncate_telegram_text(str(action.label or "").strip(), 64)
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


def _partial_delivery(
    result: TelegramSendResult, *, message_id: int = 0,
) -> TelegramSendResult:
    return TelegramSendResult(
        ok=False,
        retry_after_seconds=result.retry_after_seconds,
        error=result.error,
        status_code=result.status_code,
        partially_delivered=True,
        message_id=int(message_id or result.message_id or 0),
    )


def _send_text_result(bot, target: str, text: str, *, reply_markup=None) -> TelegramSendResult:
    chunks = split_message(text)
    sent = 0
    last_message_id = 0
    try:
        for index, chunk in enumerate(chunks):
            kwargs = {
                "reply_markup": reply_markup
            } if reply_markup is not None and index == len(chunks) - 1 else {}
            message = bot.send_message(target, chunk, **kwargs)
            try:
                last_message_id = int(getattr(message, "message_id", 0) or 0)
            except (TypeError, ValueError):
                last_message_id = 0
            sent += 1
        return TelegramSendResult(ok=True, message_id=last_message_id)
    except Exception as exc:
        result = _telegram_send_error(exc)
        if sent:
            result = TelegramSendResult(
                ok=False,
                retry_after_seconds=result.retry_after_seconds,
                error=result.error,
                status_code=result.status_code,
                partially_delivered=True,
                message_id=last_message_id,
            )
        return result


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

    response = getattr(exc, "result", None)
    status_code = positive_int(
        payload.get("error_code")
        or getattr(exc, "error_code", 0)
        or getattr(response, "status_code", 0)
    )
    if isinstance(exc, requests.ConnectTimeout):
        # 连接尚未建立，Telegram 不可能已经接收请求，可以安全重试。
        status_code = 503
    elif isinstance(exc, requests.ReadTimeout):
        # 请求可能已经到达 Telegram，但响应未返回，必须避免盲目重放。
        status_code = 408
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


def _photo_failure_allows_text_fallback(result: TelegramSendResult) -> bool:
    """仅在 Telegram 明确拒绝图片请求时回退文本，避免未知结果重复通知。"""
    status = int(result.status_code or 0)
    return 400 <= status < 500 and status not in {408, 429}


def send_result(
    text: str,
    chat_id: Optional[str] = None,
    *,
    image_url: str = "",
) -> TelegramSendResult:
    """发送兼容 HTML 结果，可选封面并保留 429 等可重试信息。"""
    bot = get_bot()
    target = str(chat_id or _chat_id or "").strip()
    if not bot or not target:
        _log_notification_fallback(text)
        return TelegramSendResult(
            ok=False, error="Telegram Bot 或 Chat ID 未配置", status_code=503,
        )
    image = str(image_url or "").strip()
    if not image:
        result = _send_text_result(bot, target, text)
        if not result.ok:
            logger.error(
                "Telegram 发送失败 status=%s retry_after=%s error=%s",
                result.status_code or "-", result.retry_after_seconds or "-",
                result.error,
            )
        return result

    chunks = split_message(text, _CAPTION_LIMIT)
    try:
        photo_message = bot.send_photo(target, image, caption=chunks[0])
        try:
            last_message_id = int(getattr(photo_message, "message_id", 0) or 0)
        except (TypeError, ValueError):
            last_message_id = 0
    except Exception as exc:
        result = _telegram_send_error(exc)
        if not _photo_failure_allows_text_fallback(result):
            logger.warning(
                "Telegram 结果封面发送结果未知，停止文本回退 "
                "type=%s status=%s",
                type(exc).__name__, result.status_code or "-",
            )
            return result
        logger.warning(
            "Telegram 明确拒绝结果封面，回退文本 type=%s status=%s",
            type(exc).__name__, result.status_code,
        )
        return _send_text_result(bot, target, text)

    try:
        for chunk in chunks[1:]:
            message = bot.send_message(target, chunk)
            try:
                last_message_id = int(getattr(message, "message_id", 0) or 0)
            except (TypeError, ValueError):
                pass
        return TelegramSendResult(ok=True, message_id=last_message_id)
    except Exception as exc:
        result = _partial_delivery(
            _telegram_send_error(exc), message_id=last_message_id,
        )
        logger.error(
            "Telegram 结果续发失败 type=%s status=%s retry_after=%s error=%s",
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


def edit_event_result(
    event: NotificationEvent, *, chat_id: str, message_id: int | str
) -> TelegramSendResult:
    """原位更新结构化消息，并保留限流、结果未知与消息身份语义。"""
    bot = get_bot()
    target = str(chat_id or "").strip()
    try:
        resolved_message_id = int(message_id)
    except (TypeError, ValueError):
        return TelegramSendResult(ok=False, error="InvalidMessageId", status_code=400)
    if not bot or not target or resolved_message_id <= 0:
        return TelegramSendResult(
            ok=False, error="Telegram Bot、Chat ID 或消息 ID 无效", status_code=503,
        )
    text = render_event(event)
    if len(split_message(text)) != 1:
        logger.warning("Telegram 原位更新内容过长，改用新消息回执")
        return TelegramSendResult(
            ok=False, error="MessageTooLongForEdit", status_code=400,
            message_id=resolved_message_id,
        )
    try:
        bot.edit_message_text(
            text, target, resolved_message_id, reply_markup=_event_markup(event)
        )
        return TelegramSendResult(ok=True, message_id=resolved_message_id)
    except Exception as exc:
        if _telegram_message_is_unchanged(exc):
            logger.debug("Telegram 原位消息已是目标内容，按幂等成功处理")
            return TelegramSendResult(ok=True, message_id=resolved_message_id)
        result = _telegram_send_error(exc)
        logger.warning(
            "Telegram 原位更新失败 type=%s status=%s",
            type(exc).__name__, result.status_code or "-",
        )
        return TelegramSendResult(
            ok=False,
            retry_after_seconds=result.retry_after_seconds,
            error=result.error,
            status_code=result.status_code,
            partially_delivered=result.partially_delivered,
            message_id=resolved_message_id,
        )


def edit_event(
    event: NotificationEvent, *, chat_id: str, message_id: int | str
) -> bool:
    """兼容既有调用者的布尔编辑接口。"""
    return bool(edit_event_result(event, chat_id=chat_id, message_id=message_id).ok)


def send_event_result(
    event: NotificationEvent, chat_id: Optional[str] = None,
) -> TelegramSendResult:
    """发送结构化事件，并保留可重试/结果未知语义。"""
    bot = get_bot()
    target = str(chat_id or _chat_id or "").strip()
    text = render_event(event)
    if not bot or not target:
        _log_notification_fallback(text)
        return TelegramSendResult(
            ok=False,
            error="Telegram Bot 或 Chat ID 未配置",
            status_code=503,
        )
    image_url = str(event.image_url or "").strip()
    reply_markup = _event_markup(event)
    if not image_url:
        result = _send_text_result(
            bot, target, text, reply_markup=reply_markup,
        )
        if not result.ok:
            logger.error(
                "Telegram 发送失败 error=%s status=%s retry_after=%s",
                result.error or "DeliveryFailed", result.status_code or "-",
                result.retry_after_seconds or "-",
            )
        return result

    caption_chunks = split_message(text, _CAPTION_LIMIT)
    try:
        photo_kwargs = {
            "reply_markup": reply_markup
        } if reply_markup is not None and len(caption_chunks) == 1 else {}
        photo_message = bot.send_photo(
            target, image_url, caption=caption_chunks[0], **photo_kwargs
        )
        try:
            last_message_id = int(getattr(photo_message, "message_id", 0) or 0)
        except (TypeError, ValueError):
            last_message_id = 0
    except Exception as exc:
        result = _telegram_send_error(exc)
        if not _photo_failure_allows_text_fallback(result):
            logger.warning(
                "Telegram 图片发送结果未知，停止文本回退 type=%s status=%s",
                type(exc).__name__, result.status_code or "-",
            )
            return result
        logger.warning(
            "Telegram 明确拒绝图片，回退文本 type=%s status=%s",
            type(exc).__name__, result.status_code,
        )
        result = _send_text_result(
            bot, target, text, reply_markup=reply_markup,
        )
        if not result.ok:
            logger.error(
                "Telegram 文本回退失败 error=%s status=%s",
                result.error or "DeliveryFailed", result.status_code or "-",
            )
        return result
    try:
        for index, chunk in enumerate(caption_chunks[1:]):
            kwargs = {
                "reply_markup": reply_markup
            } if reply_markup is not None and index == len(caption_chunks) - 2 else {}
            message = bot.send_message(target, chunk, **kwargs)
            try:
                last_message_id = int(getattr(message, "message_id", 0) or 0)
            except (TypeError, ValueError):
                pass
        return TelegramSendResult(ok=True, message_id=last_message_id)
    except Exception as exc:
        result = _partial_delivery(
            _telegram_send_error(exc), message_id=last_message_id,
        )
        logger.error(
            "Telegram 图片通知续发失败 type=%s status=%s",
            type(exc).__name__, result.status_code or "-",
        )
        return result


def send_event(event: NotificationEvent, chat_id: Optional[str] = None) -> bool:
    """兼容既有调用者的布尔接口。"""
    return bool(send_event_result(event, chat_id=chat_id).ok)


def notify_gcid_import_started(*, task_id: int, file_count: int, total_size: int,
                               retry: bool = False) -> None:
    """登记 GCID 导入事务；终态会原位更新同一条消息。"""
    from app.modules.telegram_notification_center import publish_notification_thread
    from app.modules.telegram_notification_policy import (
        NotificationImportance, NotificationTopic,
    )

    publish_notification_thread(
        f"gcid:{int(task_id)}",
        NotificationEvent(
            "GCID 重试进行中" if retry else "GCID 导入进行中",
            fields=(
                ("任务", f"#{int(task_id)}"),
                ("文件", f"{max(0, int(file_count))} 个"),
                ("体积", _format_size(max(0, int(total_size)))),
                NOTIFICATION_SECTION_BREAK,
                ("状态", "正在导入"),
            ),
            footer="完成后会更新本条消息。",
            layout="relaxed",
        ),
        topic=NotificationTopic.GCID,
        importance=NotificationImportance.RESULT,
    )


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
        path = str(_item_value(sample, "path", "") or "").strip()
        safe_name = path.replace("\\", "/").rsplit("/", 1)[-1][:180]
        if safe_name:
            lines.append(f"{safe_name}：导入失败")
    from app.modules.telegram_notification_center import publish_notification_thread
    from app.modules.telegram_notification_policy import (
        NotificationImportance, NotificationTopic,
    )

    publish_notification_thread(
        f"gcid:{int(task_id)}",
        NotificationEvent(
            title,
            fields=(
                ("任务", f"#{int(task_id)}"),
                ("成功", f"{max(0, int(success_count))} 个"),
                ("失败", f"{max(0, int(failed_count))} 个"),
            ),
            lines=tuple(lines),
            layout="relaxed",
        ),
        topic=NotificationTopic.GCID,
        importance=(
            NotificationImportance.RESULT
            if normalized == "success" else NotificationImportance.ERROR
        ),
    )


def _publish_legacy_notification(
    logical_prefix: str,
    event: NotificationEvent,
    *,
    topic: str,
) -> None:
    """兼容旧插件入口，但禁止绕过统一策略与可靠 outbox。"""
    from app.modules.telegram_notification_center import publish_notification_event
    from app.modules.telegram_notification_policy import NotificationImportance

    digest = hashlib.sha256(
        render_event(event).encode("utf-8")
    ).hexdigest()[:24]
    publish_notification_event(
        f"legacy-{logical_prefix}:{digest}",
        event,
        topic=topic,
        importance=NotificationImportance.RESULT,
    )


def notify_organize_done(summary: str) -> None:
    _publish_legacy_notification(
        "organize",
        NotificationEvent(
            "光鸭整理完成", lines=(summary,), layout="relaxed",
        ),
        topic="organize",
    )


def notify_strm_done(summary: str) -> None:
    _publish_legacy_notification(
        "strm",
        NotificationEvent(
            "STRM 同步完成", lines=(summary,), layout="relaxed",
        ),
        topic="strm",
    )


def notify_download(source: str, title: str, path: str) -> None:
    safe_name = str(path or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    _publish_legacy_notification(
        "download",
        NotificationEvent(
            "下载完成",
            fields=(("来源", source), ("任务", title), ("文件", safe_name)),
            layout="relaxed",
        ),
        topic="download",
    )


def reset() -> None:
    """清空共享客户端；调用前必须先停止旧 polling。"""
    global _bot, _chat_id, _initialized
    with _init_lock:
        _bot = None
        _chat_id = None
        _initialized = False
