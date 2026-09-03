"""TG Bot 命令处理。

命令：
- /start          开始/帮助
- /sync_gy        完整扫描并校准光鸭云盘 STRM
- /organize       选择整理光鸭云盘、本地下载或全部
- /rss            列出 RSS 订阅项
- /rss_refresh <id>  刷新指定订阅并返回新条目数
- /rss_dl <entry_id>  下载指定条目

启动条件：配置 TG_BOT_TOKEN。未配置则不启动，仅日志提示。
"""

from __future__ import annotations

import hmac
import html
import logging
import threading
import time

from app import database as db
from app.bot.progress import TelegramProgress, send_typing
from app.config import get, get_bool
from app.logger import get_logger, log_throttled
from app.notifier import (
    NOTIFICATION_SECTION_BREAK,
    NotificationEvent,
    get_bot,
    render_event,
    send_event_result,
)

logger = get_logger(__name__)

_bot = None
_bot_thread: threading.Thread | None = None
_bot_thread_stop: threading.Event | None = None
_registered_bot_id: int | None = None
_command_menu_bot_id: int | None = None
_command_menu_refresh_thread: threading.Thread | None = None
_command_menu_refresh_lock = threading.Lock()
_command_menu_refresh_requested = threading.Event()
_progress_recovery_thread: threading.Thread | None = None
_progress_recovery_stop: threading.Event | None = None
_lifecycle_lock = threading.RLock()
_lifecycle_control_lock = threading.RLock()
# 整理与同步共用锁，保证串行
_task_lock = threading.Lock()
_sync_running = False
_organize_running = False
_local_organize_running = False
_ORGANIZE_LIFECYCLE_WAIT_SECONDS = 30 * 60


def _organize_group_progress(task_state: dict) -> tuple[int, int, str, str]:
    """读取组级进度投影，返回 (当前序号, 总数, 组名, 阶段)。"""
    progress = (
        task_state.get("group_progress") if isinstance(task_state, dict) else None
    )
    if not isinstance(progress, dict):
        return 0, 0, "", ""
    try:
        index = max(0, int(progress.get("current_index") or 0))
        total = max(0, int(progress.get("total") or 0))
    except (TypeError, ValueError):
        return 0, 0, "", ""
    return (
        index,
        total,
        str(progress.get("current_group") or ""),
        str(progress.get("current_stage_label") or ""),
    )


def _organize_group_progress_line(task_state: dict) -> str:
    """按媒体目录聚合展示进度，避免按文件刷屏。"""
    index, total, name, stage = _organize_group_progress(task_state)
    if not total:
        return ""
    parts = [f"媒体目录：{index}/{total}"]
    if name:
        parts.append(html.escape(name))
    if stage:
        parts.append(html.escape(stage))
    return " · ".join(parts)


def _organize_task_active() -> bool:
    try:
        from app.modules.organize_tasks import get_organize_manager

        return get_organize_manager().task_status().get("status") in {
            "running",
            "stopping",
        }
    except Exception as exc:
        logger.warning("读取整理任务互斥状态失败 type=%s", type(exc).__name__)
        return True


def _authorized(chat_id) -> bool:
    configured = get("TG_CHAT_ID", "").strip()
    return bool(configured) and hmac.compare_digest(str(chat_id), configured)


def _configuration_complete() -> bool:
    return bool(get("TG_BOT_TOKEN", "").strip() and get("TG_CHAT_ID", "").strip())


def _reject_unauthorized(bot, msg_or_call) -> bool:
    chat = getattr(getattr(msg_or_call, "message", None), "chat", None) or getattr(
        msg_or_call, "chat", None
    )
    chat_id = getattr(chat, "id", "")
    if _authorized(chat_id):
        return False
    logger.warning(f"拒绝未授权 Telegram 会话: {chat_id}")
    try:
        if getattr(msg_or_call, "id", None):
            bot.answer_callback_query(msg_or_call.id, "未授权会话", show_alert=True)
        else:
            bot.reply_to(msg_or_call, "未授权会话")
    except Exception:
        pass
    return True


def _reject_unauthorized_group_write(bot, msg_or_call) -> bool:
    """写操作在群组中必须同时通过用户白名单，私聊保持旧行为。"""
    if _reject_unauthorized(bot, msg_or_call):
        return True
    chat_id, user_id = _telegram_identity(msg_or_call)
    if not chat_id.startswith("-"):
        return False
    from app.bot.agent_adapter import telegram_user_is_allowed

    if telegram_user_is_allowed(user_id):
        return False
    logger.warning(
        "拒绝未授权 Telegram 群组写操作: chat=%s user=%s",
        chat_id,
        user_id,
    )
    try:
        if getattr(msg_or_call, "id", None):
            bot.answer_callback_query(
                msg_or_call.id,
                "你无权在此群组执行该操作",
                show_alert=True,
            )
        else:
            bot.reply_to(msg_or_call, "你无权在此群组执行该操作")
    except Exception:
        pass
    return True


def _reject_unauthorized_resource_search(bot, msg_or_call) -> bool:
    """资源搜索在群组中额外要求用户白名单，私聊保持旧行为。"""
    if _reject_unauthorized(bot, msg_or_call):
        return True
    chat_id, user_id = _telegram_identity(msg_or_call)
    if not chat_id.startswith("-"):
        return False
    from app.bot.agent_adapter import telegram_user_is_allowed

    if telegram_user_is_allowed(user_id):
        return False
    logger.warning(
        "拒绝未授权 Telegram 群组资源搜索: chat=%s user=%s", chat_id, user_id
    )
    try:
        if getattr(msg_or_call, "id", None):
            bot.answer_callback_query(
                msg_or_call.id, "你无权在此群组使用资源搜索", show_alert=True
            )
        else:
            bot.reply_to(msg_or_call, "你无权在此群组使用资源搜索")
    except Exception:
        pass
    return True


def _configured_organize_sources() -> list[dict[str, str]]:
    """读取 Web 整理页的多源配置，，仅接受正式多源配置。"""
    from app.modules.organize_sources import normalize_organize_sources

    sources, error = normalize_organize_sources(
        get("GY_ORGANIZE_SOURCE_DIRS", ""),
    )
    if error:
        logger.warning("读取光鸭整理来源失败: %s", error)
    return sources


def _configured_local_organize_sources() -> list[object]:
    """返回可执行移动入库的本地来源；显式整理不依赖 qB 自动接管开关。"""
    sources = []
    try:
        for source in db.list_local_media_sources(owner="admin"):
            if str(getattr(source, "mode", "move")) == "preview_only":
                continue
            if not db.list_local_library_targets(source.id, owner="admin"):
                continue
            sources.append(source)
    except Exception as exc:
        logger.warning("读取本地整理来源失败 type=%s", type(exc).__name__)
    return sources


def _organize_scope_markup(
    telebot, *, chat_id: str, user_id: str, cloud: bool, local: bool
):
    """生成会话绑定的一次性整理范围选择按钮。"""
    from app.modules.telegram_write_confirmations import (
        get_telegram_write_confirmation_store,
    )

    choices: list[tuple[str, str, dict[str, str]]] = []
    if cloud:
        choices.append(("光鸭云盘", "confirm", {"scope": "guangya"}))
    if local:
        choices.append(("本地下载", "confirm", {"scope": "local"}))
    if cloud and local:
        choices.append(("全部整理", "confirm", {"scope": "all"}))
    choices.append(("取消", "cancel", {}))
    action_ids = get_telegram_write_confirmation_store().create_group(
        chat_id=chat_id,
        user_id=user_id,
        operation="organize",
        actions=[(decision, value) for _label, decision, value in choices],
    )
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        *[
            telebot.types.InlineKeyboardButton(label, callback_data=f"tgc:{action_id}")
            for (label, _decision, _value), action_id in zip(choices, action_ids)
        ]
    )
    return markup


_SHARE_PAGE_SIZE = 8
_SHARE_DIR_PAGE_SIZE = 8


def _telegram_identity(message_or_call) -> tuple[str, str]:
    message = getattr(message_or_call, "message", None) or message_or_call
    chat = getattr(message, "chat", None)
    user = getattr(message_or_call, "from_user", None) or getattr(
        message, "from_user", None
    )
    return str(getattr(chat, "id", "")), str(getattr(user, "id", ""))


_RESOURCE_PAGE_SIZE = 5
_RESOURCE_TITLE_LIMIT = 72
_RESOURCE_BUTTON_TITLE_LIMIT = 34
_RESOURCE_TARGET_TITLE_LIMIT = 180


def _resource_action_button(
    telebot,
    store,
    session_id: str,
    chat_id: str,
    user_id: str,
    text: str,
    kind: str,
    value=None,
):
    action_id = store.create_action(session_id, chat_id, user_id, kind, value)
    return telebot.types.InlineKeyboardButton(text, callback_data=f"mrs:{action_id}")


def _write_confirmation_markup(
    telebot, *, chat_id: str, user_id: str, operation: str, value: dict
):
    from app.modules.telegram_write_confirmations import (
        get_telegram_write_confirmation_store,
    )

    confirm_id, cancel_id = get_telegram_write_confirmation_store().create_pair(
        chat_id=chat_id,
        user_id=user_id,
        operation=operation,
        value=value,
    )
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        telebot.types.InlineKeyboardButton(
            "确认执行", callback_data=f"tgc:{confirm_id}"
        ),
        telebot.types.InlineKeyboardButton("取消", callback_data=f"tgc:{cancel_id}"),
    )
    return markup


def _edit_write_confirmation_message(
    bot, message, title: str, detail: str = ""
) -> bool:
    """原位封口旧命令确认卡，明确移除一次性按钮。"""
    edit = getattr(bot, "edit_message_text", None)
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    message_id = getattr(message, "message_id", None)
    if not callable(edit) or chat_id is None or message_id is None:
        return False
    text = f"<b>{html.escape(str(title))}</b>"
    if detail:
        text += f"\n{html.escape(str(detail))}"
    try:
        edit(
            text,
            chat_id,
            message_id,
            parse_mode="HTML",
            reply_markup=None,
        )
        return True
    except Exception as exc:
        logger.info("Telegram 确认卡状态更新失败 type=%s", type(exc).__name__)
        clear_markup = getattr(bot, "edit_message_reply_markup", None)
        if callable(clear_markup):
            try:
                clear_markup(chat_id, message_id, reply_markup=None)
            except Exception:
                pass
        return False


def _resource_plain_text(value, *, fallback: str = "") -> str:
    """把站点文本还原为适合 Telegram 的单行纯文本。"""
    text = html.unescape(str(value or fallback))
    return " ".join(text.split())


def _truncate_resource_text(value, limit: int, *, fallback: str = "") -> str:
    text = _resource_plain_text(value, fallback=fallback)
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _resource_item_meta(item: dict, *, include_site: bool = True) -> str:
    parts = []
    if include_site:
        parts.append(
            _resource_plain_text(
                item.get("site_name") or item.get("site_id"), fallback="未知站点"
            )
        )
    if item.get("size_text"):
        parts.append(_resource_plain_text(item["size_text"]))
    if item.get("seeders") is not None:
        parts.append(f"做种 {int(item['seeders'])}")
    published = _resource_plain_text(item.get("published_at"))
    if published:
        parts.append(published[:10])
    return " · ".join(parts)


def _resource_search_view(
    telebot,
    session_id: str,
    *,
    chat_id: str,
    user_id: str,
    site_id: str = "",
    page: int = 0,
    store=None,
):
    from app.modules.telegram_resource_search import get_telegram_resource_search_store

    store = store or get_telegram_resource_search_store()
    snapshot = store.snapshot(session_id, chat_id, user_id)
    all_items = list(snapshot["items"])
    selected_site = str(site_id or "")
    items = [
        item
        for item in all_items
        if not selected_site or str(item.get("site_id") or "") == selected_site
    ]
    pages = max(1, (len(items) + _RESOURCE_PAGE_SIZE - 1) // _RESOURCE_PAGE_SIZE)
    page = max(0, min(int(page), pages - 1))
    start = page * _RESOURCE_PAGE_SIZE
    visible = items[start : start + _RESOURCE_PAGE_SIZE]

    site_names = {
        str(site.get("site_id") or ""): str(
            site.get("site_name") or site.get("site_id") or ""
        )
        for site in snapshot["sites"]
    }
    scope_name = (
        site_names.get(selected_site, "全部站点") if selected_site else "全部站点"
    )
    query = html.escape(_resource_plain_text(snapshot["query"], fallback="未命名媒体"))
    scope = html.escape(_resource_plain_text(scope_name, fallback="全部站点"))
    lines = [
        "<b>🔎 媒体资源搜索</b>",
        f"<b>{query}</b> · {scope} · {len(items)} 项",
    ]
    if visible:
        lines.append("")
        for offset, item in enumerate(visible, start=start + 1):
            title = html.escape(
                _truncate_resource_text(
                    item.get("title"), _RESOURCE_TITLE_LIMIT, fallback="未命名资源"
                )
            )
            lines.append(f"<b>{offset}. {title}</b>")
            lines.append(html.escape(_resource_item_meta(item, include_site=False)))
    else:
        lines.extend(["", "当前站点没有匹配资源。"])

    failures = [
        site for site in snapshot["sites"] if str(site.get("status") or "") == "error"
    ]
    if failures:
        lines.append("")
        lines.append("<b>站点状态</b>")
        for site in failures:
            name = html.escape(
                str(site.get("site_name") or site.get("site_id") or "站点")
            )
            reason = html.escape(str(site.get("message") or "检索失败"))
            lines.append(f"{name}：{reason}")

    markup = telebot.types.InlineKeyboardMarkup(row_width=3)
    source_buttons = [
        _resource_action_button(
            telebot,
            store,
            session_id,
            chat_id,
            user_id,
            f"{'✓ ' if not selected_site else ''}全部 {len(all_items)}",
            "view",
            {"site_id": "", "page": 0},
        )
    ]
    for site in snapshot["sites"]:
        sid = str(site.get("site_id") or "")
        if not sid:
            continue
        suffix = (
            " !" if site.get("status") == "error" else f" {int(site.get('count') or 0)}"
        )
        label = f"{'✓ ' if sid == selected_site else ''}{site.get('site_name') or sid}{suffix}"
        source_buttons.append(
            _resource_action_button(
                telebot,
                store,
                session_id,
                chat_id,
                user_id,
                label[:32],
                "view",
                {"site_id": sid, "page": 0},
            )
        )
    markup.add(*source_buttons)

    for offset, item in enumerate(visible, start=start + 1):
        compact = _truncate_resource_text(
            item.get("title"), _RESOURCE_BUTTON_TITLE_LIMIT, fallback="未命名资源"
        )
        markup.add(
            _resource_action_button(
                telebot,
                store,
                session_id,
                chat_id,
                user_id,
                f"{offset}. {compact}",
                "item",
                {
                    "result_id": str(item.get("result_id") or ""),
                    "site_id": selected_site,
                    "page": page,
                },
            )
        )
    nav = []
    if page > 0:
        nav.append(
            _resource_action_button(
                telebot,
                store,
                session_id,
                chat_id,
                user_id,
                "上一页",
                "view",
                {"site_id": selected_site, "page": page - 1},
            )
        )
    if page + 1 < pages:
        nav.append(
            _resource_action_button(
                telebot,
                store,
                session_id,
                chat_id,
                user_id,
                "下一页",
                "view",
                {"site_id": selected_site, "page": page + 1},
            )
        )
    if nav:
        markup.add(*nav)
    if pages > 1:
        lines[1] += f" · 第 {page + 1}/{pages} 页"
    return "\n".join(lines), markup


def _resource_target_view(
    telebot,
    session_id: str,
    result_id: str,
    *,
    chat_id: str,
    user_id: str,
    site_id: str = "",
    page: int = 0,
    store=None,
):
    from app.modules.telegram_resource_search import get_telegram_resource_search_store

    store = store or get_telegram_resource_search_store()
    snapshot = store.snapshot(session_id, chat_id, user_id)
    item = next(
        (item for item in snapshot["items"] if item.get("result_id") == result_id), None
    )
    if item is None:
        raise ValueError("资源结果已失效")
    title = html.escape(
        _truncate_resource_text(
            item.get("title"), _RESOURCE_TARGET_TITLE_LIMIT, fallback="未命名资源"
        )
    )
    text = f"<b>选择下载目标</b>\n{title}\n{html.escape(_resource_item_meta(item))}"
    markup = telebot.types.InlineKeyboardMarkup(row_width=3)
    for label, target in (
        ("qBittorrent", "qb"),
        ("光鸭云盘", "guangya"),
        ("全部", "both"),
    ):
        markup.add(
            _resource_action_button(
                telebot,
                store,
                session_id,
                chat_id,
                user_id,
                label,
                "download",
                {"result_id": result_id, "target": target},
            )
        )
    markup.add(
        _resource_action_button(
            telebot,
            store,
            session_id,
            chat_id,
            user_id,
            "返回资源列表",
            "view",
            {"site_id": site_id, "page": page},
        )
    )
    return text, markup


def _share_action_button(
    telebot,
    store,
    preview_id: str,
    chat_id: str,
    user_id: str,
    text: str,
    action: str,
    value=None,
):
    action_id = store.create_action(preview_id, chat_id, user_id, action, value)
    return telebot.types.InlineKeyboardButton(text, callback_data=f"gys:{action_id}")


def _share_selection_view(
    telebot,
    preview_id: str,
    *,
    chat_id: str,
    user_id: str = "",
    page: int = 0,
    store=None,
):
    """构建分享文件分页视图；callback 只携带 opaque action ID。"""
    from app.modules.share_transfer import get_share_transfer_store

    store = store or get_share_transfer_store()
    snapshot = store.snapshot(preview_id, chat_id, user_id)
    store.begin_actions(preview_id, chat_id, user_id)
    files = list(snapshot["files"])
    pages = max(1, (len(files) + _SHARE_PAGE_SIZE - 1) // _SHARE_PAGE_SIZE)
    page = max(0, min(int(page), pages - 1))
    start = page * _SHARE_PAGE_SIZE
    selected = set(snapshot["selected_ids"])
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    for item in files[start : start + _SHARE_PAGE_SIZE]:
        marker = "[x]" if item["id"] in selected else "[ ]"
        kind = "目录" if item.get("is_dir") else "文件"
        label = f"{marker} {kind}: {str(item['name'])[:42]}"
        markup.add(
            _share_action_button(
                telebot,
                store,
                preview_id,
                chat_id,
                user_id,
                label,
                "toggle",
                {"file_id": item["id"], "page": page},
            )
        )
    nav = []
    if page > 0:
        nav.append(
            _share_action_button(
                telebot,
                store,
                preview_id,
                chat_id,
                user_id,
                "上一页",
                "files",
                page - 1,
            )
        )
    if page + 1 < pages:
        nav.append(
            _share_action_button(
                telebot,
                store,
                preview_id,
                chat_id,
                user_id,
                "下一页",
                "files",
                page + 1,
            )
        )
    if nav:
        markup.add(*nav)
    markup.add(
        _share_action_button(
            telebot, store, preview_id, chat_id, user_id, "全选", "all", page
        ),
        _share_action_button(
            telebot, store, preview_id, chat_id, user_id, "全不选", "none", page
        ),
        row_width=2,
    )
    markup.add(
        _share_action_button(
            telebot,
            store,
            preview_id,
            chat_id,
            user_id,
            f"目标目录: {snapshot['target_name']}",
            "target",
            {"parent_id": "0", "parent_name": "根目录", "page": 0, "trail": []},
        )
    )
    markup.add(
        _share_action_button(
            telebot, store, preview_id, chat_id, user_id, "确认转存", "confirm"
        ),
        _share_action_button(
            telebot, store, preview_id, chat_id, user_id, "取消", "cancel"
        ),
        row_width=2,
    )
    text = (
        "<b>光鸭分享转存</b>\n"
        f"文件页: {page + 1}/{pages}\n"
        f"已选择: {len(selected)} / {len(files)}\n"
        f"目标目录: {html.escape(str(snapshot['target_name']))}\n"
        "选择需要转存的文件或目录后确认。"
    )
    return text, markup


def _share_target_view(
    telebot,
    preview_id: str,
    *,
    chat_id: str,
    user_id: str = "",
    parent_id: str = "0",
    parent_name: str = "根目录",
    page: int = 0,
    trail=None,
    client=None,
    store=None,
):
    """构建光鸭目标目录选择器，目录 ID 仅保存在服务端 action store。"""
    from app.clients.guangya import GuangYaClient, close_guangya_client
    from app.modules.share_transfer import get_share_transfer_store

    store = store or get_share_transfer_store()
    store.snapshot(preview_id, chat_id, user_id)
    store.begin_actions(preview_id, chat_id, user_id)
    owns_client = client is None
    client = client or GuangYaClient()
    try:
        if not client.logged_in:
            raise ValueError("光鸭未登录，请先重新登录")
        directories = [
            item for item in client.list_dir(str(parent_id or "0")) if item.is_dir
        ]
    finally:
        if owns_client:
            close_guangya_client(client)
    pages = max(
        1, (len(directories) + _SHARE_DIR_PAGE_SIZE - 1) // _SHARE_DIR_PAGE_SIZE
    )
    page = max(0, min(int(page), pages - 1))
    trail = list(trail or [])
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        _share_action_button(
            telebot,
            store,
            preview_id,
            chat_id,
            user_id,
            "选择当前目录",
            "choose_target",
            {
                "target_id": str(parent_id or "0"),
                "target_name": str(parent_name or "根目录"),
            },
        )
    )
    start = page * _SHARE_DIR_PAGE_SIZE
    for item in directories[start : start + _SHARE_DIR_PAGE_SIZE]:
        markup.add(
            _share_action_button(
                telebot,
                store,
                preview_id,
                chat_id,
                user_id,
                str(item.name)[:48],
                "target",
                {
                    "parent_id": str(item.file_id),
                    "parent_name": str(item.name),
                    "page": 0,
                    "trail": trail
                    + [
                        {
                            "id": str(parent_id or "0"),
                            "name": str(parent_name or "根目录"),
                        }
                    ],
                },
            )
        )
    nav = []
    if page > 0:
        nav.append(
            _share_action_button(
                telebot,
                store,
                preview_id,
                chat_id,
                user_id,
                "上一页",
                "target",
                {
                    "parent_id": str(parent_id),
                    "parent_name": str(parent_name),
                    "page": page - 1,
                    "trail": trail,
                },
            )
        )
    if page + 1 < pages:
        nav.append(
            _share_action_button(
                telebot,
                store,
                preview_id,
                chat_id,
                user_id,
                "下一页",
                "target",
                {
                    "parent_id": str(parent_id),
                    "parent_name": str(parent_name),
                    "page": page + 1,
                    "trail": trail,
                },
            )
        )
    if nav:
        markup.add(*nav)
    if trail:
        previous = trail[-1]
        markup.add(
            _share_action_button(
                telebot,
                store,
                preview_id,
                chat_id,
                user_id,
                "上级目录",
                "target",
                {
                    "parent_id": previous["id"],
                    "parent_name": previous["name"],
                    "page": 0,
                    "trail": trail[:-1],
                },
            )
        )
    markup.add(
        _share_action_button(
            telebot,
            store,
            preview_id,
            chat_id,
            user_id,
            "返回文件选择",
            "files",
            0,
        )
    )
    text = (
        "<b>选择目标目录</b>\n"
        f"目标目录: {html.escape(str(parent_name or '根目录'))}\n"
        f"目录页: {page + 1}/{pages}"
    )
    return text, markup


def _recover_stale_progress_until_delivered(
    bot,
    telebot_module,
    stop_event: threading.Event,
    *,
    operation_ids: tuple[str, ...],
    delays: tuple[float, ...] = (2.0, 8.0, 30.0, 120.0, 300.0),
) -> int:
    """持续重试启动前遗留回执；只处理调用方同步截取的启动快照。"""
    from app.bot.progress import (
        pending_stale_operation_count,
        recover_stale_operations,
    )

    if not operation_ids:
        return 0
    total_recovered = 0
    attempt = 0
    while not stop_event.is_set():
        total_recovered += recover_stale_operations(
            bot,
            telebot_module,
            operation_ids=operation_ids,
        )
        pending = pending_stale_operation_count(operation_ids)
        if pending == 0:
            break
        delay = delays[min(attempt, len(delays) - 1)] if delays else 30.0
        attempt += 1
        logger.info(
            "Telegram 中断任务仍有 %s 项未收尾，%.0f 秒后继续重试",
            pending if pending >= 0 else "未知",
            delay,
        )
        if stop_event.wait(max(0.1, float(delay))):
            break
    return total_recovered


def init_bot(stop_event: threading.Event | None = None):
    """初始化并注册共享 Bot。Token/Chat ID 不完整时保持停用。"""
    global _bot, _registered_bot_id, _progress_recovery_thread, _progress_recovery_stop
    with _lifecycle_lock:
        if stop_event is not None and stop_event.is_set():
            return None
        if not _configuration_complete():
            logger.warning("Telegram 配置不完整，Bot polling 不启动")
            _bot = None
            return None
        bot = get_bot()
        if bot is None:
            logger.warning("未配置 TG_BOT_TOKEN，Bot polling 不启动")
            _bot = None
            return None
        if _registered_bot_id != id(bot):
            try:
                import telebot

                _register_commands(bot, telebot)
                from app.bot.progress import (
                    pending_stale_operation_ids,
                    reset_terminal_delivery_retry_generation,
                )

                reset_terminal_delivery_retry_generation()
                # 必须在 init_bot 返回、polling 开始接收新任务前同步截取快照。
                # 每代恢复线程持有独立停止事件，避免旧线程在重启清理事件后复活。
                operation_ids = pending_stale_operation_ids()
                previous_recovery_stop = _progress_recovery_stop
                if previous_recovery_stop is not None:
                    previous_recovery_stop.set()
                recovery_stop = threading.Event()
                _progress_recovery_stop = recovery_stop

                if operation_ids:

                    def recover_progress() -> None:
                        recovered = _recover_stale_progress_until_delivered(
                            bot,
                            telebot,
                            recovery_stop,
                            operation_ids=operation_ids,
                        )
                        if recovered:
                            logger.info("已收尾 %s 个中断的 Telegram 长任务", recovered)

                    _progress_recovery_thread = threading.Thread(
                        target=recover_progress,
                        name="tg-progress-recovery",
                        daemon=True,
                    )
                    _progress_recovery_thread.start()
                else:
                    _progress_recovery_thread = None
                _registered_bot_id = id(bot)
            except Exception as exc:
                logger.error("TG Bot 命令注册失败 type=%s", type(exc).__name__)
                return None
        _bot = bot
        logger.info("TG Bot 已初始化")
        return bot


def _maintenance_task_busy() -> bool:
    """统一判断会与光鸭、本地整理或 STRM 同步互斥的后台任务。"""
    return (
        _sync_running
        or _organize_running
        or _local_organize_running
        or _organize_task_active()
    )


def _start_sync_gy(bot, telebot, source_message) -> bool:
    """在确认回调后启动 STRM 同步，并保证启动失败不会泄漏互斥锁。"""
    global _sync_running
    if _maintenance_task_busy():
        bot.reply_to(source_message, "已有整理或同步任务在执行，请稍后再试")
        return False
    if not _task_lock.acquire(blocking=False):
        bot.reply_to(source_message, "任务队列占用中，请稍后")
        return False
    _sync_running = True
    progress = None
    try:
        sync_label = "光鸭 STRM 完整同步"
        progress = TelegramProgress(
            bot,
            telebot,
            source_message.chat.id,
            sync_label,
            source_message=source_message,
            timeout_seconds=4 * 60 * 60,
        ).begin("<b>正在完整同步光鸭 STRM</b>\n正在并发扫描全部云端目录并比对本地索引…")
        threading.Thread(
            target=_do_sync,
            args=(source_message.chat.id, progress, "full"),
            name="tg-sync-gy",
            daemon=False,
        ).start()
        return True
    except Exception as exc:
        logger.warning("Telegram STRM 同步启动失败 type=%s", type(exc).__name__)
        if progress is not None:
            progress.finish(f"<b>{sync_label}启动失败</b>\n任务未启动，请稍后重试。")
        else:
            bot.reply_to(source_message, f"{sync_label}未能启动，请稍后重试")
        _sync_running = False
        _task_lock.release()
        return False


def _start_guangya_organize(bot, telebot, source_message) -> bool:
    """在确认回调后按当前配置启动光鸭整理。"""
    global _organize_running
    sources = _configured_organize_sources()
    dst = get("GY_ORGANIZE_TARGET_DIR", "").strip()
    if not sources or not dst or dst == "0":
        bot.reply_to(
            source_message,
            "未配置整理源目录或目标目录\n"
            "请在控制台「网盘整理」页选择至少一个源目录和归档目标目录。",
        )
        return False
    if _maintenance_task_busy():
        bot.reply_to(source_message, "已有整理或同步任务在执行，请稍后再试")
        return False
    if not _task_lock.acquire(blocking=False):
        bot.reply_to(source_message, "任务队列占用中，请稍后")
        return False
    _organize_running = True
    progress = None
    try:
        source_names = "、".join(source["name"] for source in sources)
        progress = TelegramProgress(
            bot,
            telebot,
            source_message.chat.id,
            "光鸭整理",
            source_message=source_message,
            timeout_seconds=4 * 60 * 60,
        ).begin(
            "<b>正在启动光鸭整理</b>\n"
            f"来源：{html.escape(source_names)}\n"
            "正在校验目录与整理规则…"
        )
        threading.Thread(
            target=_do_organize,
            args=(source_message.chat.id, sources, dst, progress),
            name="tg-organize-gy",
            daemon=True,
        ).start()
        return True
    except Exception as exc:
        logger.warning("Telegram 光鸭整理启动失败 type=%s", type(exc).__name__)
        if progress is not None:
            progress.finish("<b>光鸭整理启动失败</b>\n任务未启动，请稍后重试。")
        else:
            bot.reply_to(source_message, "光鸭整理未能启动，请稍后重试")
        _organize_running = False
        _task_lock.release()
        return False


def _start_organize_local(bot, telebot, source_message) -> bool:
    """启动一次性本地来源扫描，并等待本批任务形成合并结果。"""
    global _local_organize_running
    if not _configured_local_organize_sources():
        bot.reply_to(
            source_message,
            "没有可整理的本地来源\n请先配置本地下载路径和至少一个媒体库归档目标。",
        )
        return False
    if _maintenance_task_busy():
        bot.reply_to(source_message, "已有整理或同步任务在执行，请稍后再试")
        return False
    if not _task_lock.acquire(blocking=False):
        bot.reply_to(source_message, "任务队列占用中，请稍后")
        return False
    _local_organize_running = True
    progress = None
    try:
        progress = TelegramProgress(
            bot,
            telebot,
            source_message.chat.id,
            "本地下载整理",
            source_message=source_message,
            timeout_seconds=4 * 60 * 60,
        ).begin(
            "<b>正在扫描本地下载目录</b>\n"
            "将发现已有媒体，并立即按当前整理规则检查与归档。"
        )
        threading.Thread(
            target=_do_organize_local,
            args=(source_message.chat.id, progress),
            name="tg-organize-local",
            daemon=True,
        ).start()
        return True
    except Exception as exc:
        logger.warning("Telegram 本地整理启动失败 type=%s", type(exc).__name__)
        if progress is not None:
            progress.finish("<b>本地下载整理启动失败</b>\n任务未启动，请稍后重试。")
        else:
            bot.reply_to(source_message, "本地下载整理未能启动，请稍后重试")
        _local_organize_running = False
        if _task_lock.locked():
            _task_lock.release()
        return False


def _start_organize_all(bot, telebot, source_message) -> bool:
    """顺序执行光鸭整理和本地下载整理，并只发送一条合并结果。"""
    global _organize_running, _local_organize_running
    sources = _configured_organize_sources()
    dst = get("GY_ORGANIZE_TARGET_DIR", "").strip()
    if not sources or not dst or dst == "0" or not _configured_local_organize_sources():
        bot.reply_to(
            source_message,
            "全部整理配置不完整\n"
            "请同时配置光鸭整理来源/目标，以及本地下载来源/媒体库目标。",
        )
        return False
    if _maintenance_task_busy():
        bot.reply_to(source_message, "已有整理或同步任务在执行，请稍后再试")
        return False
    if not _task_lock.acquire(blocking=False):
        bot.reply_to(source_message, "任务队列占用中，请稍后")
        return False
    _organize_running = True
    _local_organize_running = True
    progress = None
    try:
        progress = TelegramProgress(
            bot,
            telebot,
            source_message.chat.id,
            "全部整理",
            source_message=source_message,
            timeout_seconds=8 * 60 * 60,
        ).begin(
            "<b>正在启动全部整理</b>\n"
            "阶段 1/2：光鸭云盘；完成后将继续整理本地下载目录。"
        )
        threading.Thread(
            target=_do_organize_all,
            args=(source_message.chat.id, sources, dst, progress),
            name="tg-organize-all",
            daemon=True,
        ).start()
        return True
    except Exception as exc:
        logger.warning("Telegram 全部整理启动失败 type=%s", type(exc).__name__)
        if progress is not None:
            progress.finish("<b>全部整理启动失败</b>\n任务未启动，请稍后重试。")
        else:
            bot.reply_to(source_message, "全部整理未能启动，请稍后重试")
        _organize_running = False
        _local_organize_running = False
        if _task_lock.locked():
            _task_lock.release()
        return False


def _telegram_agent_available() -> bool:
    return get_bool("AGENT_ENABLED", False) and get_bool("TG_AGENT_ENABLED", False)


def _command_menu_specs() -> list[tuple[str, str]]:
    commands = [
        ("organize", "整理光鸭云盘或本地媒体"),
        ("sync_gy", "同步光鸭云盘STRM"),
        ("media_search", "搜索媒体资源"),
        ("rss", "查看RSS订阅"),
        ("rss_refresh", "刷新RSS订阅"),
        ("rss_dl", "下载RSS条目"),
    ]
    commands.append(("agent", "管理 Media Agent"))
    if _telegram_agent_available():
        commands.append(("agent_reset", "重置 Agent 会话"))
    commands.extend(
        [
            ("status", "查看运行状态"),
            ("help", "查看使用帮助"),
            ("start", "开始"),
        ]
    )
    return commands


def _set_command_menu(bot, telebot) -> bool:
    global _command_menu_bot_id
    try:
        bot.set_my_commands(
            [
                telebot.types.BotCommand(command, description)
                for command, description in _command_menu_specs()
            ]
        )
    except Exception as exc:
        if _command_menu_bot_id == id(bot):
            _command_menu_bot_id = None
        logger.warning("设置命令菜单失败 type=%s", type(exc).__name__)
        return False
    _command_menu_bot_id = id(bot)
    return True


def _refresh_command_menu_worker() -> None:
    global _command_menu_refresh_thread
    rerun = False
    try:
        while True:
            _command_menu_refresh_requested.clear()
            bot = _bot
            if bot is not None:
                import telebot

                for attempt, delay in enumerate((0.0, 1.0, 3.0), start=1):
                    if delay and _command_menu_refresh_requested.wait(delay):
                        break
                    if _set_command_menu(bot, telebot):
                        break
                    if attempt < 3:
                        logger.info(
                            "Telegram 命令菜单将在后台重试 attempt=%s", attempt + 1
                        )
            with _command_menu_refresh_lock:
                if _command_menu_refresh_requested.is_set():
                    continue
                _command_menu_refresh_thread = None
                return
    except Exception as exc:
        logger.warning("刷新 Telegram 命令菜单失败 type=%s", type(exc).__name__)
    finally:
        with _command_menu_refresh_lock:
            if _command_menu_refresh_thread is threading.current_thread():
                _command_menu_refresh_thread = None
            rerun = _command_menu_refresh_requested.is_set()
        if rerun:
            request_command_menu_refresh()


def request_command_menu_refresh() -> bool:
    """后台刷新命令菜单，避免配置保存等待 Telegram 网络请求。"""
    global _command_menu_refresh_thread
    _command_menu_refresh_requested.set()
    with _command_menu_refresh_lock:
        if (
            _command_menu_refresh_thread is not None
            and _command_menu_refresh_thread.is_alive()
        ):
            return False
        thread = threading.Thread(
            target=_refresh_command_menu_worker,
            name="telegram-command-menu-refresh",
            daemon=True,
        )
        _command_menu_refresh_thread = thread
        thread.start()
        return True


def _ensure_command_menu(bot) -> bool:
    if _command_menu_bot_id == id(bot):
        return True
    import telebot

    return _set_command_menu(bot, telebot)


def _register_commands(bot, telebot):
    def require_auth(handler):
        def wrapped(msg, *args, **kwargs):
            if _reject_unauthorized(bot, msg):
                return None
            return handler(msg, *args, **kwargs)

        return wrapped

    def require_write_auth(handler):
        def wrapped(msg, *args, **kwargs):
            if _reject_unauthorized_group_write(bot, msg):
                return None
            return handler(msg, *args, **kwargs)

        return wrapped

    @bot.message_handler(commands=["help"])
    @bot.message_handler(commands=["start"])
    @require_auth
    def cmd_start(msg):
        sections = [
            "<b>MediaFlux Bot</b>\n\n"
            "发送磁力、ED2K、HTTP(S) 链接或 .torrent 文件，"
            "可选择推送到光鸭、qBittorrent 或两者。",
            "<b>资源搜索</b>\n"
            "/media_search 片名 — 搜索媒体资源\n"
            "/媒体搜索 片名 — 搜索媒体资源",
            "<b>整理与同步</b>\n"
            "/sync_gy — 完整扫描并校准光鸭 STRM\n"
            "/organize — 选择整理光鸭云盘、本地下载或全部",
            "<b>RSS 订阅</b>\n"
            "/rss — 查看订阅\n"
            "/rss_refresh ID — 刷新订阅\n"
            "/rss_dl ID — 下载条目",
        ]
        agent_lines = [
            "<b>Media Agent</b>",
            "/agent — 查看状态并开启或关闭 Agent",
        ]
        if _telegram_agent_available():
            agent_lines.append("/agent_reset — 重置 Agent 会话")
        sections.append("\n".join(agent_lines))
        sections.append("<b>运行状态</b>\n/status — 查看整理、同步与待处理状态")
        bot.reply_to(msg, "\n\n".join(sections))

    @bot.message_handler(commands=["status"])
    @require_auth
    def cmd_status(msg):
        try:
            from app.modules.organize_tasks import get_organize_manager

            organize = get_organize_manager().task_status()
            organize_status = str(organize.get("status") or "idle")
            organize_label = {
                "running": "整理中",
                "stopping": "正在停止",
                "completed": "最近任务已完成",
                "partial": "最近任务部分完成",
                "failed": "最近任务失败",
                "stopped": "最近任务已停止",
                "idle": "空闲",
            }.get(organize_status, organize_status)
            current = str(organize.get("current_source") or "").strip()
            pending = db.count_download_requests_requiring_attention()
            lines = [
                "<b>MediaFlux 运行状态</b>",
                f"光鸭整理：{html.escape(organize_label)}",
                f"本地整理：{'运行中' if _local_organize_running else '空闲'}",
                f"STRM 同步：{'运行中' if _sync_running else '空闲'}",
                f"下载待处理：{pending} 项",
            ]
            if current:
                lines.insert(2, f"当前目录：{html.escape(current)}")
            group_line = _organize_group_progress_line(organize)
            if group_line:
                lines.insert(3 if current else 2, group_line)
            bot.reply_to(msg, "\n".join(lines))
        except Exception as exc:
            logger.warning("Telegram 状态查询失败 type=%s", type(exc).__name__)
            bot.reply_to(msg, "状态读取失败，请稍后重试")

    @bot.message_handler(commands=["agent"])
    @require_auth
    def cmd_agent(msg):
        from app.bot.agent_adapter import handle_agent_guide

        handle_agent_guide(bot, msg, telebot)

    @bot.message_handler(commands=["agent_reset"])
    @require_auth
    def cmd_agent_reset(msg):
        from app.bot.agent_adapter import handle_agent_reset

        handle_agent_reset(bot, msg)

    def matches_command(msg, command: str) -> bool:
        text = str(getattr(msg, "text", "") or "").strip()
        if not text:
            return False
        token = text.split(maxsplit=1)[0].split("@", 1)[0].casefold()
        return token == f"/{command.casefold()}"

    def start_resource_search(msg):
        text = str(getattr(msg, "text", "") or "").strip()
        query = (
            text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
        )
        if not query:
            bot.reply_to(
                msg,
                "<b>请输入要搜索的媒体名称</b>\n"
                "例如：/media_search 光阴之外\n"
                "也可以发送：/媒体搜索 光阴之外\n\n"
                "搜索完成后，可直接回复“下载第 2 个”。",
            )
            return
        if not get_bool("INDEXER_SEARCH_ENABLED"):
            bot.reply_to(msg, "资源站搜索当前已关闭，请先在设置中启用")
            return
        progress = TelegramProgress(
            bot,
            telebot,
            msg.chat.id,
            f"资源搜索：{query}",
            source_message=msg,
            timeout_seconds=150,
        ).begin(
            f"<b>正在搜索资源</b>\n关键词：{html.escape(query)}\n正在连接已启用的资源站…"
        )
        threading.Thread(
            target=_run_telegram_resource_search,
            args=(bot, msg, telebot, query, progress),
            name="tg-media-search",
            daemon=True,
        ).start()

    @bot.message_handler(commands=["media_search"])
    @bot.message_handler(
        func=lambda msg: matches_command(msg, "media_search"),
        content_types=["text"],
    )
    def cmd_media_search(msg):
        if _reject_unauthorized_resource_search(bot, msg):
            return
        start_resource_search(msg)

    @bot.message_handler(
        func=lambda msg: matches_command(msg, "媒体搜索"),
        content_types=["text"],
    )
    def cmd_media_search_cn(msg):
        if _reject_unauthorized_resource_search(bot, msg):
            return
        start_resource_search(msg)

    @bot.message_handler(commands=["sync_gy"])
    @require_write_auth
    def cmd_sync(msg):
        if _maintenance_task_busy():
            bot.reply_to(msg, "已有整理或同步任务在执行，请稍后再试")
            return
        chat_id, user_id = _telegram_identity(msg)
        markup = _write_confirmation_markup(
            telebot,
            chat_id=chat_id,
            user_id=user_id,
            operation="sync_gy",
            value={},
        )
        bot.reply_to(
            msg,
            "<b>确认同步光鸭 STRM</b>\n"
            "将按当前并发设置（默认 15 个扫描线程）遍历全部已配置目录、校准 STRM、执行安全清理并按需刷新媒体库。",
            parse_mode="HTML",
            reply_markup=markup,
        )

    @bot.message_handler(commands=["organize"])
    @require_write_auth
    def cmd_organize_scope(msg):
        if _maintenance_task_busy():
            bot.reply_to(msg, "已有整理或同步任务在执行，请稍后再试")
            return
        cloud_sources = _configured_organize_sources()
        cloud_target = get("GY_ORGANIZE_TARGET_DIR", "").strip()
        cloud_available = bool(cloud_sources and cloud_target and cloud_target != "0")
        local_sources = _configured_local_organize_sources()
        local_available = bool(local_sources)
        if not cloud_available and not local_available:
            bot.reply_to(
                msg,
                "尚未配置可执行的整理来源\n"
                "请先配置光鸭整理目录，或本地下载来源和媒体库目标。",
            )
            return
        chat_id, user_id = _telegram_identity(msg)
        markup = _organize_scope_markup(
            telebot,
            chat_id=chat_id,
            user_id=user_id,
            cloud=cloud_available,
            local=local_available,
        )
        available = []
        if cloud_available:
            available.append(f"光鸭 {len(cloud_sources)} 个来源")
        if local_available:
            available.append(f"本地 {len(local_sources)} 个来源")
        bot.reply_to(
            msg,
            "<b>选择整理范围</b>\n"
            f"当前可用：{html.escape(' · '.join(available))}\n"
            "本地整理会扫描下载目录中已经存在的媒体；全部整理将先执行光鸭，再执行本地。",
            parse_mode="HTML",
            reply_markup=markup,
        )

    @bot.message_handler(commands=["rss"])
    @require_auth
    def cmd_rss(msg):
        from app import database as db

        rows = db.list_rss_subscriptions()
        if not rows:
            bot.reply_to(msg, "暂无 RSS 订阅项\n\n请在控制台「RSS 订阅」页添加订阅源")
            return
        lines = ["<b>RSS 订阅列表</b>\n"]
        for r in rows:
            enabled = "启用" if r["enabled"] else "停用"
            action = "自动下载" if r["action"] == "download" else "手动"
            lines.append(
                f"[{enabled}] <b>#{r['id']} {html.escape(str(r['name']))}</b> [{action}]"
            )
        lines.append("\n/rss_refresh ID - 刷新订阅\n/rss_dl ID - 下载条目")
        bot.reply_to(msg, "\n".join(lines))

    @bot.message_handler(commands=["rss_refresh"])
    @bot.message_handler(
        func=lambda msg: matches_command(msg, "rss_refresh"),
        content_types=["text"],
    )
    @require_write_auth
    def cmd_rss_refresh(msg):
        logger.info("收到 Telegram RSS 刷新命令 chat=%s", getattr(msg.chat, "id", ""))
        parts = str(getattr(msg, "text", "") or "").split()
        if len(parts) < 2:
            rows = db.list_rss_subscriptions()
            if not rows:
                bot.reply_to(msg, "暂无 RSS 订阅\n请先在控制台「RSS 订阅」页添加订阅源")
                return
            lines = ["<b>请选择要刷新的订阅</b>"]
            for row in rows[:8]:
                state = "启用" if row["enabled"] else "停用"
                lines.append(
                    f"/rss_refresh {int(row['id'])} — "
                    f"{html.escape(str(row['name']))}（{state}）"
                )
            if len(rows) > 8:
                lines.append(f"\n另有 {len(rows) - 8} 个订阅，可发送 /rss 查看完整列表")
            bot.reply_to(msg, "\n".join(lines))
            return
        try:
            sid = int(parts[1])
        except ValueError:
            bot.reply_to(msg, "订阅ID 必须是数字")
            return
        row = db.get_rss_subscription(sid)
        if row is None:
            bot.reply_to(msg, "未找到该 RSS 订阅，请发送 /rss 查看有效 ID")
            return
        from app.modules.rss import rss_subscription_refresh_revision

        chat_id, user_id = _telegram_identity(msg)
        markup = _write_confirmation_markup(
            telebot,
            chat_id=chat_id,
            user_id=user_id,
            operation="rss_refresh",
            value={
                "subscription_id": sid,
                "expected_revision": rss_subscription_refresh_revision(row),
            },
        )
        bot.reply_to(
            msg,
            "<b>确认刷新 RSS 订阅</b>\n"
            f"订阅：#{sid} {html.escape(str(row['name'] or '未命名订阅'))}\n"
            "将拉取订阅并写入新条目；此操作本身不会提交下载。",
            reply_markup=markup,
        )

    @bot.message_handler(commands=["rss_dl"])
    @bot.message_handler(
        func=lambda msg: matches_command(msg, "rss_dl"),
        content_types=["text"],
    )
    @require_write_auth
    def cmd_rss_dl(msg):
        logger.info("收到 Telegram RSS 下载命令 chat=%s", getattr(msg.chat, "id", ""))
        parts = str(getattr(msg, "text", "") or "").split()
        if len(parts) < 2:
            rows = db.list_rss_entries(status="pending", limit=6)
            if not rows:
                bot.reply_to(
                    msg,
                    "暂无待下载的 RSS 条目\n可先发送 /rss 查看订阅，再刷新对应订阅。",
                )
                return
            lines = ["<b>请选择要下载的条目</b>"]
            for row in rows:
                title = " ".join(str(row["title"] or "未命名条目").split())
                if len(title) > 42:
                    title = title[:41].rstrip() + "…"
                lines.append(f"/rss_dl {int(row['id'])} — {html.escape(title)}")
            lines.append("\n发送上面的命令即可提交下载。")
            bot.reply_to(msg, "\n".join(lines))
            return
        try:
            eid = int(parts[1])
        except ValueError:
            bot.reply_to(msg, "条目ID 必须是数字")
            return
        row = db.get_rss_entry(eid)
        if row is None:
            bot.reply_to(msg, "未找到该 RSS 条目，请发送 /rss_dl 查看待下载 ID")
            return
        title = " ".join(str(row["title"] or "未命名条目").split())
        if len(title) > 80:
            title = title[:79].rstrip() + "…"
        chat_id, user_id = _telegram_identity(msg)
        markup = _write_confirmation_markup(
            telebot,
            chat_id=chat_id,
            user_id=user_id,
            operation="rss_download",
            value={"entry_id": eid},
        )
        bot.reply_to(
            msg,
            "<b>确认提交 RSS 下载</b>\n"
            f"条目：#{eid} {html.escape(title)}\n"
            "确认后将按该订阅配置提交到下载后端。",
            reply_markup=markup,
        )

    def send_target_picker(
        msg,
        request_id: int,
        title: str,
        *,
        allow_guangya: bool = True,
        chat_id: str = "",
        user_id: str = "",
        reissued: bool = False,
    ):
        from app.modules.telegram_write_confirmations import (
            get_telegram_write_confirmation_store,
        )

        if not chat_id or not user_id:
            chat_id, user_id = _telegram_identity(msg)
        choices = [
            ("光鸭云盘", "confirm", {"request_id": request_id, "target": "guangya"}),
            ("qBittorrent", "confirm", {"request_id": request_id, "target": "qb"}),
            ("两者", "confirm", {"request_id": request_id, "target": "both"}),
            ("取消", "cancel", {"request_id": request_id}),
        ]
        if not allow_guangya:
            choices = [choices[1], choices[3]]
        action_ids = get_telegram_write_confirmation_store().create_group(
            chat_id=chat_id,
            user_id=user_id,
            operation="download_request",
            actions=[(decision, value) for _label, decision, value in choices],
        )
        keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            *[
                telebot.types.InlineKeyboardButton(
                    label, callback_data=f"tgc:{action_id}"
                )
                for (label, _decision, _value), action_id in zip(choices, action_ids)
            ]
        )
        bot.reply_to(
            msg,
            "<b>选择下载目标</b>\n"
            f"任务: {html.escape(title or '未命名任务')}\n"
            + (
                "原确认已失效，已为你重新生成一次性按钮。"
                if reissued
                else "相同链接或种子只会创建一份请求。"
            ),
            reply_markup=keyboard,
        )

    def reissue_pending_picker(msg, request: dict, title: str) -> bool:
        if str(request.get("status") or "") != "pending":
            return False
        chat_id, user_id = _telegram_identity(msg)
        row = db.bind_pending_download_request_owner(
            int(request["id"]),
            chat_id=chat_id,
            user_id=user_id,
        )
        if row is None:
            return False
        send_target_picker(
            msg,
            int(row["id"]),
            str(row["title"] or title),
            chat_id=chat_id,
            user_id=user_id,
            reissued=True,
        )
        return True

    @bot.message_handler(content_types=["document"])
    @require_write_auth
    def receive_torrent(msg):
        document = msg.document
        filename = str(document.file_name or "")
        mime = str(document.mime_type or "").lower()
        if not filename.lower().endswith(".torrent") and "bittorrent" not in mime:
            bot.reply_to(msg, "暂不支持该文件，仅可发送 .torrent 种子文件")
            return
        try:
            from app.modules.download_dispatcher import (
                create_request,
                torrent_download_input,
            )

            send_typing(
                bot,
                msg.chat.id,
                message_thread_id=getattr(msg, "message_thread_id", None),
            )
            file_info = bot.get_file(document.file_id)
            data = bot.download_file(file_info.file_path)
            item = torrent_download_input(filename, data)
            _chat_id, user_id = _telegram_identity(msg)
            request = create_request(
                item,
                str(msg.chat.id),
                str(msg.message_id),
                user_id=user_id,
            )
            if not request["created"]:
                if reissue_pending_picker(msg, request, item.title):
                    return
                bot.reply_to(
                    msg,
                    f"该种子已存在，请勿重复提交（请求 #{request['id']}，状态 {request['status']}）",
                )
                return
            send_target_picker(msg, request["id"], item.title)
        except ValueError as exc:
            logger.info("Telegram 种子校验失败 type=%s", type(exc).__name__)
            bot.reply_to(msg, f"种子文件无效：{html.escape(str(exc))}")
        except Exception as exc:
            logger.error("Telegram 种子解析失败 type=%s", type(exc).__name__)
            bot.reply_to(msg, "种子文件处理失败，请确认文件完整后重试")

    @bot.message_handler(
        func=lambda msg: (
            bool(getattr(msg, "text", "")) and not msg.text.startswith("/")
        ),
        content_types=["text"],
    )
    @require_auth
    def receive_link(msg):
        from app.modules.download_dispatcher import (
            create_request,
            extract_download_url,
            normalize_download_url,
            route_download_url,
        )

        url = extract_download_url(msg.text)
        if not url:
            from app.bot.agent_adapter import handle_agent_message

            handle_agent_message(bot, telebot, msg)
            return
        try:
            route = route_download_url(url)
            if route == "web":
                from app.bot.agent_adapter import handle_agent_message

                handle_agent_message(bot, telebot, msg)
                return
            if _reject_unauthorized_group_write(bot, msg):
                return
            send_typing(
                bot,
                msg.chat.id,
                message_thread_id=getattr(msg, "message_thread_id", None),
            )
            if route == "guangya_share":
                _inspect_telegram_share(bot, msg, url, telebot)
                return
            item = normalize_download_url(url)
            _chat_id, user_id = _telegram_identity(msg)
            request = create_request(
                item,
                str(msg.chat.id),
                str(msg.message_id),
                user_id=user_id,
            )
            if not request["created"]:
                if reissue_pending_picker(msg, request, item.title):
                    return
                bot.reply_to(
                    msg,
                    f"该任务已存在，请勿重复提交（请求 #{request['id']}，状态 {request['status']}）",
                )
                return
            send_target_picker(msg, request["id"], item.title)
        except ValueError as exc:
            logger.info("Telegram 下载链接校验失败 type=%s", type(exc).__name__)
            bot.reply_to(msg, f"下载链接无效：{html.escape(str(exc))}")
        except Exception as exc:
            logger.warning("Telegram 链接处理失败 (%s)", type(exc).__name__)
            bot.reply_to(msg, "链接处理失败，请检查链接后重试")

    @bot.callback_query_handler(
        func=lambda call: str(getattr(call, "data", "")).startswith("tgc:")
    )
    def choose_write_confirmation(call):
        if _reject_unauthorized_group_write(bot, call):
            return
        _handle_write_confirmation_callback(bot, call, telebot)

    @bot.callback_query_handler(
        func=lambda call: str(getattr(call, "data", "")).startswith("agk:")
    )
    def choose_agent_action(call):
        if _reject_unauthorized(bot, call):
            return
        from app.bot.agent_adapter import handle_agent_callback

        handle_agent_callback(bot, call, telebot)

    @bot.callback_query_handler(
        func=lambda call: str(getattr(call, "data", "")).startswith("agp:")
    )
    def choose_agent_patrol_action(call):
        if _reject_unauthorized(bot, call):
            return
        from app.bot.agent_adapter import handle_agent_patrol_callback

        handle_agent_patrol_callback(bot, call, telebot)

    @bot.callback_query_handler(
        func=lambda call: str(getattr(call, "data", "")).startswith("gys:")
    )
    def choose_share_transfer(call):
        if _reject_unauthorized_group_write(bot, call):
            return
        _handle_share_callback(bot, call, telebot)

    @bot.callback_query_handler(
        func=lambda call: str(getattr(call, "data", "")).startswith("orgc:")
    )
    def choose_organize_candidate(call):
        if _reject_unauthorized_group_write(bot, call):
            return
        _handle_organize_confirmation_callback(bot, call)

    @bot.callback_query_handler(
        func=lambda call: str(getattr(call, "data", "")).startswith("mrs:")
    )
    def choose_media_resource(call):
        if _reject_unauthorized_resource_search(bot, call):
            return
        _handle_resource_search_callback(bot, call, telebot)

    @bot.callback_query_handler(
        func=lambda call: str(getattr(call, "data", "")).startswith("dl:")
    )
    def choose_download_target(call):
        if _reject_unauthorized_group_write(bot, call):
            return
        parts = str(getattr(call, "data", "") or "").split(":")
        if len(parts) != 3 or not parts[1].isdigit():
            bot.answer_callback_query(
                call.id,
                "该按钮已失效，请重新发送链接或种子",
                show_alert=True,
            )
            return
        chat_id, user_id = _telegram_identity(call)
        row = db.bind_pending_download_request_owner(
            int(parts[1]),
            chat_id=chat_id,
            user_id=user_id,
        )
        if row is None:
            bot.answer_callback_query(
                call.id,
                "该请求已处理或不属于你",
                show_alert=True,
            )
            return
        bot.answer_callback_query(call.id, "已生成新的确认按钮")
        send_target_picker(
            call.message,
            int(row["id"]),
            str(row["title"] or "未命名任务"),
            chat_id=chat_id,
            user_id=user_id,
            reissued=True,
        )
        _edit_write_confirmation_message(
            bot,
            call.message,
            "旧下载确认已失效",
            "新的单次确认按钮已发送，请使用最新消息继续操作。",
        )

    _set_command_menu(bot, telebot)


def _run_rss_refresh(
    bot, msg, sid: int, progress: TelegramProgress | None, expected_revision: str = ""
) -> None:
    try:
        from app.modules.rss import RSSEngine

        result = RSSEngine().refresh(sid, expected_revision=expected_revision)
        if result.get("busy"):
            text = html.escape(str(result.get("error") or "该订阅正在刷新"))
        elif result.get("error"):
            text = f"<b>RSS 刷新失败</b>\n{html.escape(str(result['error']))}"
        else:
            partial = bool(result.get("partial"))
            failed_sources = max(0, int(result.get("failed_sources", 0) or 0))
            text = (
                f"<b>RSS 刷新{'部分完成' if partial else '完成'}</b>\n"
                f"订阅：#{sid}\n"
                f"拉取：{int(result.get('total', 0) or 0)}\n"
                f"新增：{int(result.get('new', 0) or 0)}\n"
                f"排除：{int(result.get('skipped', 0) or 0)}"
            )
            if partial:
                text += (
                    f"\n暂不可用源：{failed_sources}"
                    "\n其余订阅源已处理，请稍后核对失败源。"
                )
    except Exception as exc:
        logger.error("Telegram RSS 刷新失败 sub#%s type=%s", sid, type(exc).__name__)
        text = "刷新失败，请稍后重试"
    if progress is not None:
        progress.finish(text)
    else:
        bot.reply_to(msg, text)


def _run_rss_download(bot, msg, eid: int, progress: TelegramProgress | None) -> None:
    try:
        from app.modules.rss import RSSEngine

        result = RSSEngine().download(eid)
        if result.get("review_required"):
            detail = html.escape(
                str(
                    result.get("error")
                    or "提交结果待核对，请先检查下载器状态，勿直接重复提交"
                )
            )
            text = f"<b>RSS 下载结果待核对</b>\n{detail}"
        elif result.get("error"):
            text = f"<b>RSS 下载提交失败</b>\n{html.escape(str(result['error']))}"
        elif result.get("ok"):
            method = html.escape(str(result.get("method") or "下载器"))
            text = f"<b>RSS 下载已提交</b>\n条目：#{eid}\n目标：{method}"
        else:
            text = "<b>RSS 下载提交失败</b>\n请检查下载器配置与资源地址。"
    except Exception as exc:
        logger.error("Telegram RSS 下载失败 entry#%s type=%s", eid, type(exc).__name__)
        text = "<b>RSS 下载提交失败</b>\n下载器或资源暂时不可用，请稍后重试。"
    if progress is not None:
        progress.finish(text)
    else:
        bot.reply_to(msg, text)


def _run_telegram_resource_search(
    bot, msg, telebot, query: str, progress: TelegramProgress | None
) -> None:
    from app.modules.telegram_resource_search import (
        get_telegram_indexer_worker,
        get_telegram_resource_search_store,
    )

    chat_id, user_id = _telegram_identity(msg)
    try:
        result = get_telegram_indexer_worker().search(query)
        store = get_telegram_resource_search_store()
        session_id = store.create_session(
            chat_id=chat_id,
            user_id=user_id,
            query=str(result.get("query") or query),
            items=list(result.get("items") or []),
            sites=list(result.get("sites") or []),
        )
        text, markup = _resource_search_view(
            telebot,
            session_id,
            chat_id=chat_id,
            user_id=user_id,
            store=store,
        )
        if progress is not None:
            progress.finish(text, reply_markup=markup)
        else:
            bot.reply_to(msg, text, reply_markup=markup)
    except Exception as exc:
        logger.warning("Telegram 媒体资源搜索失败 type=%s", type(exc).__name__)
        text = "媒体资源搜索失败，请稍后重试"
        try:
            if progress is not None:
                progress.finish(text)
            else:
                bot.reply_to(msg, text)
        except Exception:
            pass


def _handle_resource_search_callback(bot, call, telebot) -> None:
    from app.modules.telegram_resource_search import (
        TelegramResourceSearchError,
        get_telegram_resource_search_store,
    )

    chat_id, user_id = _telegram_identity(call)
    try:
        _prefix, action_id = str(call.data or "").split(":", 1)
        store = get_telegram_resource_search_store()
        action = store.resolve_action(action_id, chat_id, user_id)
        session_id = str(action["session_id"])
        kind = str(action["kind"])
        value = action.get("value") if isinstance(action.get("value"), dict) else {}
        if kind == "view":
            text, markup = _resource_search_view(
                telebot,
                session_id,
                chat_id=chat_id,
                user_id=user_id,
                site_id=str(value.get("site_id") or ""),
                page=int(value.get("page") or 0),
                store=store,
            )
            bot.answer_callback_query(call.id, "已切换资源范围")
            bot.edit_message_text(
                text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup,
            )
            return
        if kind == "item":
            result_id = str(value.get("result_id") or "")
            if not result_id:
                raise TelegramResourceSearchError("资源结果已失效，请重新搜索")
            text, markup = _resource_target_view(
                telebot,
                session_id,
                result_id,
                chat_id=chat_id,
                user_id=user_id,
                site_id=str(value.get("site_id") or ""),
                page=int(value.get("page") or 0),
                store=store,
            )
            bot.answer_callback_query(call.id, "请选择下载目标")
            bot.edit_message_text(
                text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup,
            )
            return
        if kind == "download":
            result_id = str(value.get("result_id") or "")
            target = str(value.get("target") or "")
            if not result_id or target not in {"qb", "guangya", "both"}:
                raise TelegramResourceSearchError("下载参数无效")
            snapshot = store.snapshot(session_id, chat_id, user_id)
            item = next(
                (
                    candidate
                    for candidate in snapshot["items"]
                    if str(candidate.get("result_id") or "") == result_id
                ),
                None,
            )
            if item is None:
                raise TelegramResourceSearchError("资源结果已失效，请重新搜索")
            labels = {
                "qb": "qBittorrent",
                "guangya": "光鸭云盘",
                "both": "全部目标",
            }
            markup = _write_confirmation_markup(
                telebot,
                chat_id=chat_id,
                user_id=user_id,
                operation="resource_download",
                value={"result_id": result_id, "target": target},
            )
            bot.answer_callback_query(call.id, "请确认下载操作")
            bot.edit_message_text(
                "<b>确认提交资源下载</b>\n"
                f"资源：{html.escape(_truncate_resource_text(item.get('title'), 120, fallback='未命名资源'))}\n"
                f"目标：{html.escape(labels[target])}\n"
                "确认后将创建下载请求并连接对应后端。",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup,
            )
            return
        raise TelegramResourceSearchError("不支持的操作")
    except (TelegramResourceSearchError, ValueError, TypeError) as exc:
        bot.answer_callback_query(call.id, str(exc), show_alert=True)
    except Exception as exc:
        logger.warning("Telegram 媒体资源操作失败 type=%s", type(exc).__name__)
        bot.answer_callback_query(call.id, "操作失败，请重新搜索", show_alert=True)


def _handle_write_confirmation_callback(bot, call, telebot) -> None:
    from app.modules.telegram_write_confirmations import (
        TelegramWriteConfirmationError,
        get_telegram_write_confirmation_store,
    )

    claimed = False
    confirmation_progress = None
    try:
        action_id = str(getattr(call, "data", ""))[4:]
        chat_id, user_id = _telegram_identity(call)
        action = get_telegram_write_confirmation_store().claim(
            action_id,
            chat_id=chat_id,
            user_id=user_id,
        )
        claimed = True
        operation = str(action["operation"])
        value = action["value"] if isinstance(action.get("value"), dict) else {}
        if operation == "agent_control":
            from app.bot.agent_adapter import handle_agent_control_action

            handle_agent_control_action(bot, call, telebot, action)
            return
        if operation == "download_request":
            request_id = int(value.get("request_id"))
            row = db.bind_pending_download_request_owner(
                request_id,
                chat_id=chat_id,
                user_id=user_id,
            )
            if row is None:
                _edit_write_confirmation_message(
                    bot,
                    call.message,
                    "下载请求已失效",
                    "该请求已处理或不属于当前会话。",
                )
                bot.answer_callback_query(
                    call.id,
                    "该请求已处理或不属于你",
                    show_alert=True,
                )
                return
            if action["decision"] == "cancel":
                if not db.claim_download_request(request_id, "cancelled"):
                    _edit_write_confirmation_message(
                        bot, call.message, "下载请求已处理", "请勿重复操作。"
                    )
                    bot.answer_callback_query(call.id, "该请求已处理", show_alert=True)
                    return
                db.update_download_request(
                    request_id,
                    status="cancelled",
                    completed_at=db.now(),
                )
                bot.answer_callback_query(call.id, "下载请求已取消")
                if callable(getattr(bot, "edit_message_text", None)):
                    bot.edit_message_text(
                        "<b>下载请求已取消</b>",
                        call.message.chat.id,
                        call.message.message_id,
                        reply_markup=None,
                    )
                return
            if str(row["kind"] or "") == "http":
                from app.modules.download_dispatcher import route_download_url

                if route_download_url(str(row["source_value"] or "")) == "web":
                    if db.claim_download_request(request_id, "cancelled"):
                        db.update_download_request(
                            request_id,
                            status="cancelled",
                            error="普通网页链接未提交下载",
                            completed_at=db.now(),
                        )
                    _edit_write_confirmation_message(
                        bot,
                        call.message,
                        "未提交下载",
                        "该地址是普通网页，不是可识别的下载直链。",
                    )
                    bot.answer_callback_query(
                        call.id,
                        "普通网页不会创建下载任务",
                        show_alert=True,
                    )
                    return
            target = str(value.get("target") or "")
            if target not in {"qb", "guangya", "both"}:
                raise TelegramWriteConfirmationError("下载确认参数无效")
            _edit_write_confirmation_message(
                bot, call.message, "下载请求已确认", "正在提交到所选下载目标…"
            )
            bot.answer_callback_query(call.id, "正在提交下载任务")
            threading.Thread(
                target=_dispatch_download_callback,
                args=(
                    bot,
                    call.message.chat.id,
                    call.message.message_id,
                    request_id,
                    target,
                ),
                name=f"tg-download-{request_id}",
                daemon=True,
            ).start()
            return
        if action["decision"] == "cancel":
            bot.answer_callback_query(call.id, "操作已取消")
            _edit_write_confirmation_message(bot, call.message, "操作已取消")
            return
        if operation == "sync_gy":
            _edit_write_confirmation_message(
                bot, call.message, "STRM 同步已确认", "正在执行完整同步…"
            )
            bot.answer_callback_query(call.id, "已确认，开始完整同步")
            if not _start_sync_gy(bot, telebot, call.message):
                _edit_write_confirmation_message(
                    bot,
                    call.message,
                    "STRM 同步未启动",
                    "请根据提示检查配置或稍后重试。",
                )
            return
        if operation == "organize":
            scope = str(value.get("scope") or "")
            labels = {"guangya": "光鸭整理", "local": "本地下载整理", "all": "全部整理"}
            starter = {
                "guangya": _start_guangya_organize,
                "local": _start_organize_local,
                "all": _start_organize_all,
            }.get(scope)
            if starter is None:
                raise TelegramWriteConfirmationError("整理范围无效")
            _edit_write_confirmation_message(
                bot, call.message, f"{labels[scope]}已确认", "正在创建整理任务…"
            )
            bot.answer_callback_query(call.id, f"已确认，开始{labels[scope]}")
            if not starter(bot, telebot, call.message):
                _edit_write_confirmation_message(
                    bot,
                    call.message,
                    f"{labels[scope]}未启动",
                    "请根据提示检查配置或稍后重试。",
                )
            return
        if operation == "resource_download":
            result_id = str(value.get("result_id") or "")
            target = str(value.get("target") or "")
            if not result_id or target not in {"qb", "guangya", "both"}:
                raise TelegramWriteConfirmationError("下载确认参数无效")
            _edit_write_confirmation_message(
                bot, call.message, "资源下载已确认", "正在提交到所选下载目标…"
            )
            bot.answer_callback_query(call.id, "正在提交下载任务")
            threading.Thread(
                target=_dispatch_resource_download,
                args=(
                    bot,
                    call.message.chat.id,
                    call.message.message_id,
                    result_id,
                    target,
                    user_id,
                ),
                name="tg-media-download",
                daemon=True,
            ).start()
            return
        if operation == "rss_refresh":
            subscription_id = int(value.get("subscription_id"))
            expected_revision = str(value.get("expected_revision") or "")
            _edit_write_confirmation_message(
                bot, call.message, "RSS 刷新已确认", f"正在刷新订阅 #{subscription_id}…"
            )
            progress = TelegramProgress(
                bot,
                telebot,
                call.message.chat.id,
                f"RSS 订阅 #{subscription_id} 刷新",
                source_message=call.message,
                timeout_seconds=150,
            ).begin(
                f"<b>正在刷新 RSS 订阅</b>\n订阅：#{subscription_id}\n"
                "完成后会在本消息返回结果。"
            )
            confirmation_progress = progress
            bot.answer_callback_query(call.id, "已确认，开始刷新")
            if callable(getattr(bot, "send_message", None)):
                threading.Thread(
                    target=_run_rss_refresh,
                    args=(
                        bot,
                        call.message,
                        subscription_id,
                        progress,
                        expected_revision,
                    ),
                    name=f"tg-rss-refresh-{subscription_id}",
                    daemon=True,
                ).start()
            else:
                _run_rss_refresh(
                    bot,
                    call.message,
                    subscription_id,
                    progress,
                    expected_revision,
                )
            progress.dismiss_source_message()
            return
        if operation == "rss_download":
            entry_id = int(value.get("entry_id"))
            _edit_write_confirmation_message(
                bot, call.message, "RSS 下载已确认", f"正在提交条目 #{entry_id}…"
            )
            progress = TelegramProgress(
                bot,
                telebot,
                call.message.chat.id,
                f"RSS 条目 #{entry_id} 下载",
                source_message=call.message,
                timeout_seconds=150,
            ).begin(
                f"<b>正在提交 RSS 下载</b>\n条目：#{entry_id}\n"
                "正在解析资源并连接下载器…"
            )
            confirmation_progress = progress
            bot.answer_callback_query(call.id, "已确认，开始提交")
            if callable(getattr(bot, "send_message", None)):
                threading.Thread(
                    target=_run_rss_download,
                    args=(bot, call.message, entry_id, progress),
                    name=f"tg-rss-download-{entry_id}",
                    daemon=True,
                ).start()
            else:
                _run_rss_download(bot, call.message, entry_id, progress)
            progress.dismiss_source_message()
            return
        raise TelegramWriteConfirmationError("不支持的确认操作")
    except (TelegramWriteConfirmationError, TypeError, ValueError) as exc:
        message = str(exc)
        if claimed or "已过期或已处理" in message:
            _edit_write_confirmation_message(
                bot, call.message, "确认已失效", message or "请重新发起操作。"
            )
        bot.answer_callback_query(call.id, message, show_alert=True)
    except Exception as exc:
        logger.warning("Telegram 写操作确认失败 type=%s", type(exc).__name__)
        if confirmation_progress is not None:
            confirmation_progress.finish(
                "<b>操作未启动</b>\n创建后台任务失败，请稍后重新发起。"
            )
        if claimed:
            _edit_write_confirmation_message(
                bot, call.message, "操作未启动", "处理确认时发生异常，请重新发起。"
            )
        bot.answer_callback_query(call.id, "操作失败，请重新发起", show_alert=True)


def _dispatch_resource_download(
    bot,
    chat_id,
    message_id,
    result_id: str,
    target: str,
    user_id: str = "",
) -> None:
    from app.modules.download_tracker import get_download_tracker
    from app.modules.telegram_resource_search import get_telegram_indexer_worker

    try:
        result = get_telegram_indexer_worker().download(
            result_id,
            target,
            chat_id=str(chat_id),
            user_id=str(user_id),
            message_id=str(message_id),
        )
        labels = {"qb": "qBittorrent", "guangya": "光鸭云盘"}
        succeeded_items = list(result.get("succeeded") or [])
        failed_items = list(result.get("failed") or [])
        succeeded = "、".join(labels.get(item, item) for item in succeeded_items)
        failed = "、".join(labels.get(item, item) for item in failed_items)
        status = str(result.get("status") or "failed")
        if status == "duplicate":
            text = "<b>该资源已提交</b>\n请勿重复创建下载任务。"
        elif result.get("ok"):
            text = (
                "<b>下载任务已提交</b>\n"
                f"请求：#{int(result.get('request_id') or 0)}\n"
                f"成功：{html.escape(succeeded or '无')}"
            )
            if failed:
                text += f"\n失败：{html.escape(failed)}"
            text += "\n" + _download_follow_up_text(succeeded_items)
            get_download_tracker().reload()
        else:
            text = "下载提交失败\n" + html.escape(
                str(result.get("error") or "请检查下载器配置")
            )
        bot.edit_message_text(text, chat_id, message_id, reply_markup=None)
    except Exception as exc:
        logger.warning("Telegram 资源站下载失败 type=%s", type(exc).__name__)
        try:
            bot.edit_message_text(
                "下载处理失败，请稍后重试", chat_id, message_id, reply_markup=None
            )
        except Exception:
            pass


def _handle_organize_confirmation_callback(bot, call) -> None:
    from app.modules.organize_confirmations import (
        cancel_confirmation,
        skip_confirmation,
        start_confirmation,
    )

    chat_id, _user_id = _telegram_identity(call)
    try:
        _prefix, token, selection = str(call.data or "").split(":", 2)
        if not token:
            raise ValueError("确认参数无效")
        if selection in {"cancel", "skip"}:
            if selection == "skip":
                skip_confirmation(
                    token,
                    chat_id=chat_id,
                    message_id=call.message.message_id,
                )
                callback_text = "已跳过，本次待确认结束"
            else:
                cancel_confirmation(
                    token,
                    chat_id=chat_id,
                    message_id=call.message.message_id,
                )
                callback_text = "已保留待确认文件"
            try:
                bot.answer_callback_query(call.id, callback_text)
            except Exception as exc:
                logger.info(
                    "Telegram 确认终止回调应答失败 type=%s",
                    type(exc).__name__,
                )
            return
        selected_index = int(selection)
        db.bind_organize_confirmation_message(
            token, chat_id=chat_id, message_id=call.message.message_id
        )
        result = start_confirmation(token, selected_index, chat_id=chat_id)
        candidate = result.get("candidate") or {}
        provider = str(candidate.get("provider") or "").strip().lower()
        tmdb_id_raw = str(candidate.get("tmdb_id") or "").strip()
        external_id_raw = str(candidate.get("external_id") or tmdb_id_raw).strip()
        if not provider and tmdb_id_raw:
            provider = "tmdb"
        title = str(candidate.get("title") or external_id_raw or "候选媒体")
        year = str(candidate.get("year") or "")
        external_id = external_id_raw
        display = f"{title} ({year})" if year else title
        status = str(result.get("status") or "queued")
        replayed = bool(result.get("replayed"))
        queue_position = max(0, int(result.get("queue_position") or 0))
        if status == "running":
            status_text = "正在执行"
            callback_text = "该任务正在执行" if replayed else "已开始确认整理"
        elif status == "completed":
            status_text = "已完成"
            callback_text = "该任务已完成"
        else:
            status_text = (
                f"等待执行 · 前方 {queue_position} 项"
                if queue_position
                else "等待执行 · 等待当前整理完成"
            )
            callback_text = "该任务已在队列中" if replayed else "已加入整理队列"
        bot.answer_callback_query(call.id, callback_text)
        media_type = str(result.get("media_type") or candidate.get("media_type") or "")
        media_label = (
            "成人内容"
            if provider in {"metatube", "clean_title"}
            else "剧集"
            if media_type == "tv"
            else "电影"
        )
        scope_summary = str(result.get("scope_summary") or "")
        source_name = str(result.get("source_name") or result.get("directory") or "")
        identity_label = (
            "MetaTube"
            if provider == "metatube"
            else "清洗标题"
            if provider == "clean_title"
            else "TMDB"
        )
        detail_fields: list[tuple[object, object]] = [
            ("目标媒体", display),
            (
                "类型",
                f"{media_label}" + (f" · {scope_summary}" if scope_summary else ""),
            ),
            (identity_label, external_id),
            ("涉及文件", f"{int(result.get('file_count') or 0)} 个视频"),
        ]
        if source_name:
            detail_fields.append(("存储来源", source_name))
        detail_fields.extend(
            (
                NOTIFICATION_SECTION_BREAK,
                ("队列", str(result.get("task_id") or "")),
                ("处理状态", status_text),
            )
        )
        bot.edit_message_text(
            render_event(
                NotificationEvent(
                    f"✅ 已选择：{display}",
                    fields=tuple(detail_fields),
                    footer="可以继续选择其他待确认媒体，系统会按点击顺序串行整理。",
                    layout="relaxed",
                )
            ),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=None,
        )
    except (TypeError, ValueError) as exc:
        bot.answer_callback_query(call.id, str(exc or "确认参数无效"), show_alert=True)
    except Exception as exc:
        logger.warning("Telegram 整理候选处理失败 type=%s", type(exc).__name__)
        bot.answer_callback_query(
            call.id, "确认整理提交失败，请稍后重试", show_alert=True
        )


def _inspect_telegram_share(bot, msg, share_url: str, telebot) -> None:
    from app.modules.share_transfer import (
        get_share_transfer_store,
        inspect_share_for_transfer,
    )

    chat_id, user_id = _telegram_identity(msg)
    try:
        preview = inspect_share_for_transfer(
            share_url,
            chat_id,
            user_id,
            store=get_share_transfer_store(),
        )
        text, markup = _share_selection_view(
            telebot,
            preview["preview_id"],
            chat_id=chat_id,
            user_id=user_id,
            store=get_share_transfer_store(),
        )
        bot.reply_to(msg, text, reply_markup=markup)
    except ValueError:
        bot.reply_to(msg, "光鸭分享链接无效，请检查链接或提取码")
    except Exception as exc:
        logger.warning("Telegram 光鸭分享解析失败 (%s)", type(exc).__name__)
        bot.reply_to(msg, "光鸭分享解析失败，请检查链接或提取码")


def _edit_share_message(bot, call, text: str, markup=None) -> None:
    bot.edit_message_text(
        text,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup,
    )


def _handle_share_callback(bot, call, telebot) -> None:
    from app.modules.share_transfer import get_share_transfer_store

    store = get_share_transfer_store()
    chat_id, user_id = _telegram_identity(call)
    try:
        _prefix, action_id = str(call.data or "").split(":", 1)
        resolved = store.resolve_action(action_id, chat_id, user_id)
        preview_id = resolved["preview_id"]
        action = resolved["action"]
        value = resolved.get("value")

        if action == "cancel":
            store.discard(preview_id, chat_id, user_id)
            bot.answer_callback_query(call.id, "已取消")
            _edit_share_message(bot, call, "光鸭分享转存已取消")
            return
        if action == "toggle":
            value = value if isinstance(value, dict) else {}
            store.toggle(preview_id, str(value.get("file_id") or ""), chat_id, user_id)
            page = int(value.get("page") or 0)
            text, markup = _share_selection_view(
                telebot,
                preview_id,
                chat_id=chat_id,
                user_id=user_id,
                page=page,
                store=store,
            )
        elif action == "all":
            store.select_all(preview_id, chat_id, user_id)
            text, markup = _share_selection_view(
                telebot,
                preview_id,
                chat_id=chat_id,
                user_id=user_id,
                page=int(value or 0),
                store=store,
            )
        elif action == "none":
            store.select_none(preview_id, chat_id, user_id)
            text, markup = _share_selection_view(
                telebot,
                preview_id,
                chat_id=chat_id,
                user_id=user_id,
                page=int(value or 0),
                store=store,
            )
        elif action == "files":
            text, markup = _share_selection_view(
                telebot,
                preview_id,
                chat_id=chat_id,
                user_id=user_id,
                page=int(value or 0),
                store=store,
            )
        elif action == "target":
            value = value if isinstance(value, dict) else {}
            text, markup = _share_target_view(
                telebot,
                preview_id,
                chat_id=chat_id,
                user_id=user_id,
                parent_id=str(value.get("parent_id") or "0"),
                parent_name=str(value.get("parent_name") or "根目录"),
                page=int(value.get("page") or 0),
                trail=value.get("trail") or [],
                store=store,
            )
        elif action == "choose_target":
            value = value if isinstance(value, dict) else {}
            store.set_target(
                preview_id,
                str(value.get("target_id") or "0"),
                str(value.get("target_name") or "根目录"),
                chat_id,
                user_id,
            )
            text, markup = _share_selection_view(
                telebot,
                preview_id,
                chat_id=chat_id,
                user_id=user_id,
                store=store,
            )
        elif action == "confirm":
            snapshot = store.snapshot(preview_id, chat_id, user_id)
            selected_ids = list(snapshot["selected_ids"])
            locked = store.consume(
                preview_id,
                chat_id,
                user_id,
                selected_ids=selected_ids,
                target_id=snapshot["target_id"],
                target_name=snapshot["target_name"],
            )
            bot.answer_callback_query(call.id, "正在转存")
            _edit_share_message(
                bot,
                call,
                f"正在转存 {len(locked.selected_ids)} 项到 {html.escape(locked.target_name)}",
            )
            threading.Thread(
                target=_dispatch_share_transfer_callback,
                args=(
                    bot,
                    call.message.chat.id,
                    call.message.message_id,
                    preview_id,
                    selected_ids,
                    locked.target_id,
                    locked.target_name,
                    chat_id,
                    user_id,
                ),
                name=f"tg-share-{preview_id[:8]}",
                daemon=True,
            ).start()
            return
        else:
            raise ValueError("操作已过期或无效")
        bot.answer_callback_query(call.id)
        _edit_share_message(bot, call, text, markup)
    except (TypeError, ValueError) as exc:
        bot.answer_callback_query(
            call.id, str(exc) or "操作已过期或无效", show_alert=True
        )
    except Exception as exc:
        logger.warning("Telegram 光鸭分享操作失败 (%s)", type(exc).__name__)
        bot.answer_callback_query(
            call.id, "操作失败，请重新解析分享链接", show_alert=True
        )


def _dispatch_share_transfer_callback(
    bot,
    message_chat_id,
    message_id,
    preview_id: str,
    selected_ids: list[str],
    target_id: str,
    target_name: str,
    owner_chat_id: str,
    owner_user_id: str,
) -> None:
    from app.modules.share_transfer import (
        create_share_request,
        get_share_transfer_store,
    )

    try:
        result = create_share_request(
            preview_id,
            selected_ids,
            target_id,
            owner_chat_id,
            user_id=owner_user_id,
            target_name=target_name,
            origin="telegram",
            tracker_chat_id=str(message_chat_id),
            store=get_share_transfer_store(),
        )
        if result.get("duplicate") and result.get("accepted"):
            text = (
                "<b>相同光鸭分享转存请求正在处理</b>\n"
                f"请求: #{result.get('request_id')}\n"
                f"状态: {html.escape(str(result.get('status') or 'pending'))}"
            )
        elif result.get("success"):
            duplicate = "（幂等复用已有请求）" if result.get("duplicate") else ""
            text = (
                "<b>光鸭分享转存完成</b>\n"
                f"请求: #{result.get('request_id')} {duplicate}\n"
                f"项目: {result.get('count', len(selected_ids))}\n"
                f"目标: {html.escape(str(target_name or '根目录'))}"
            )
            organize_target = str(get("GY_ORGANIZE_TARGET_DIR", "") or "").strip()
            if organize_target and organize_target != "0" and str(target_id) != "0":
                text += "\n已交由现有 tracker 按配置处理整理、STRM 与媒体库刷新。"
            else:
                text += "\n当前配置不触发后续整理链。"
        else:
            text = "光鸭分享转存失败\n" + html.escape(
                str(result.get("error") or "请稍后查看任务状态")
            )
        bot.edit_message_text(text, message_chat_id, message_id)
    except Exception as exc:
        logger.warning("Telegram 光鸭分享转存提交失败 (%s)", type(exc).__name__)
        try:
            bot.edit_message_text(
                "光鸭分享转存失败，请稍后查看任务状态", message_chat_id, message_id
            )
        except Exception:
            pass


def _download_follow_up_text(succeeded: list[str]) -> str:
    follow_up = ["系统将持续跟踪下载进度。"]
    if "guangya" in succeeded:
        target = get("GY_ORGANIZE_TARGET_DIR", "").strip()
        if target and target != "0":
            follow_up.append("光鸭任务完成后将尝试启动整理、STRM 同步和媒体库刷新。")
        else:
            follow_up.append("光鸭任务完成后仅通知状态；当前未配置整理目标目录。")
    if "qb" in succeeded:
        enabled = bool(db.list_local_media_sources(owner="admin", enabled_only=True))
        if enabled:
            follow_up.append(
                "qBittorrent 任务完成后将按本地媒体来源执行识别、移动和媒体库刷新。"
            )
        else:
            follow_up.append(
                "qBittorrent 任务完成后仅通知状态；当前没有启用的本地媒体来源。"
            )
    return "\n".join(follow_up)


def _dispatch_download_callback(
    bot, chat_id, message_id, request_id: int, target: str
) -> None:
    try:
        from app.modules.download_dispatcher import dispatch_request
        from app.modules.download_tracker import get_download_tracker

        result = dispatch_request(request_id, target)
        if result.get("ok"):
            labels = {"qb": "qBittorrent", "guangya": "光鸭云盘"}
            succeeded_items = list(result.get("succeeded") or [])
            succeeded = "、".join(labels.get(item, item) for item in succeeded_items)
            failed = "、".join(
                labels.get(item, item) for item in result.get("failed", [])
            )
            text = (
                f"<b>下载任务已提交</b>\n请求: #{request_id}\n成功: {succeeded or '无'}"
            )
            if failed:
                text += f"\n失败: {failed}\n{html.escape(result.get('error') or '')}"
            text += "\n" + _download_follow_up_text(succeeded_items)
            bot.edit_message_text(text, chat_id, message_id, reply_markup=None)
            get_download_tracker().reload()
        else:
            bot.edit_message_text(
                f"下载提交失败\n{html.escape(result.get('error') or '未知错误')}",
                chat_id,
                message_id,
                reply_markup=None,
            )
    except Exception as exc:
        logger.error(
            "Telegram 下载分流失败 request#%s type=%s",
            request_id,
            type(exc).__name__,
        )
        try:
            bot.edit_message_text(
                "下载提交异常，请检查下载器连接与任务配置后重试",
                chat_id,
                message_id,
                reply_markup=None,
            )
        except Exception:
            pass


_MAX_STRM_PROGRESS_EDITS = 6


def _make_strm_progress_editor(bot, chat_id, progress_handle):
    edits = 0
    seen_stages: set[str] = set()
    labels = {
        "scan": "扫描云端目录",
        "generate": "生成 STRM",
        "metadata": "同步元数据",
        "cleanup": "清理索引",
        "refresh": "刷新媒体库",
        "complete": "完成",
        "failed": "失败",
        "retry": "重试失败项",
    }

    def update(stage, completed, total, detail):
        nonlocal edits
        if (
            not progress_handle
            or edits >= _MAX_STRM_PROGRESS_EDITS
            or stage in seen_stages
        ):
            return
        seen_stages.add(str(stage))
        bounded_total = max(1, int(total or 0))
        percent = max(0, min(100, int(int(completed or 0) * 100 / bounded_total)))
        text = f"光鸭 STRM 同步中\n阶段: {labels.get(str(stage), str(detail or stage))}\n进度: {percent}%"
        try:
            updater = getattr(progress_handle, "update", None)
            if callable(updater):
                updater(html.escape(text))
            else:
                bot.edit_message_text(text, chat_id, progress_handle)
            edits += 1
        except Exception as exc:
            logger.warning("Telegram STRM 进度编辑失败 type=%s", type(exc).__name__)

    return update


def _strm_count(stats: dict, key: str) -> int:
    try:
        return max(0, int(stats.get(key, 0) or 0))
    except (TypeError, ValueError):
        return 0


def _strm_seconds(stats: dict, key: str) -> float:
    try:
        return max(0.0, float(stats.get(key, 0) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _strm_cleanup_text(stats: dict) -> str:
    return (
        f"🗑 {_strm_count(stats, 'cleaned'):,} 无效 STRM ｜ "
        f"📁 {_strm_count(stats, 'empty_dirs_cleaned'):,} 空目录 ｜ "
        f"🗃 {_strm_count(stats, 'metadata_cleaned'):,} 失效元数据"
    )


def _strm_result_status(
    stats: dict, *, partial: bool = False, stopped: bool = False
) -> str:
    if stopped or stats.get("stopped"):
        return "⏹️ 同步已停止"
    if (
        partial
        or _strm_count(stats, "failed")
        or _strm_count(stats, "metadata_failed")
        or stats.get("scan_incomplete")
        or stats.get("clean_skipped")
        or stats.get("fallback_required")
    ):
        return "⚠️ 部分完成"
    return "✅ 同步完成"


def _strm_source_report_line(source: dict) -> str:
    stats = source.get("stats") if isinstance(source.get("stats"), dict) else {}
    name = str(source.get("name") or source.get("id") or "未命名来源").strip()
    if len(name) > 48:
        name = f"{name[:45]}..."

    details = [f"{_strm_count(stats, 'scanned_files'):,} 文件"]
    changes = (
        ("新建", _strm_count(stats, "created")),
        ("更新", _strm_count(stats, "updated")),
        ("跳过", _strm_count(stats, "skipped")),
        ("失败", _strm_count(stats, "failed")),
    )
    changed = [f"{value:,} {label}" for label, value in changes if value]
    details.extend(changed or ["无变化"])

    metadata_generated = _strm_count(stats, "metadata_generated")
    metadata_queued = _strm_count(stats, "metadata_queued")
    if metadata_generated:
        details.append(f"{metadata_generated:,} 元数据更新")
    if metadata_queued:
        details.append(f"{metadata_queued:,} 元数据排队")

    source_seconds = (
        _strm_seconds(stats, "scan_elapsed_seconds")
        + _strm_seconds(stats, "generate_elapsed_seconds")
        + _strm_seconds(stats, "metadata_elapsed_seconds")
        + _strm_seconds(stats, "cleanup_elapsed_seconds")
    )
    display_name = f"#{name}" if name.isdigit() and not name.startswith("#") else name
    return f"└ • {display_name}：{' · '.join(details)} ({source_seconds:.2f}s)"


def _strm_summary_event(
    result: dict,
    stats: dict,
    source_count: int,
    source_results: list[dict] | None = None,
) -> NotificationEvent:
    refresh = result.get("media_refresh")
    refresh_text = "未启用或本轮无变化"
    if isinstance(refresh, dict) and refresh:
        refresh_text = " / ".join(
            f"{name} {'刷新成功 🎯' if ok else '刷新失败 ❌'}"
            for name, ok in refresh.items()
        )
    try:
        elapsed = max(0.0, float(result.get("elapsed_seconds", 0) or 0.0))
    except (TypeError, ValueError):
        elapsed = 0.0
    partial = bool(result.get("partial"))
    stopped = bool(result.get("stopped"))

    lines: list[str] = []
    sources = [source for source in (source_results or []) if isinstance(source, dict)]
    source_overview = ""
    if sources:
        visible_sources = sources[:12]
        source_lines = [_strm_source_report_line(source) for source in visible_sources]
        omitted = len(sources) - len(visible_sources)
        if omitted:
            source_lines.append(
                f"另有 {omitted:,} 个来源已折叠，完整结果请查看 Web 运行记录。"
            )
        source_overview = "\n".join(source_lines)
    if stats.get("clean_skipped"):
        lines.append(
            "部分来源检测到扫描或一致性异常；为避免误删，已跳过对应来源的失效清理。"
        )

    removed_changes = sum(
        1
        for item in (stats.get("changes") or [])
        if isinstance(item, dict)
        and str(item.get("action") or "") in {"removed", "removed_dir"}
    )
    footer = ""
    if removed_changes:
        footer = f"本轮 {removed_changes:,} 条清理明细已记录到 Web 运行记录。"

    fields: list[tuple[object, object]] = [
        (
            "状态",
            f"{_strm_result_status(stats, partial=partial, stopped=stopped)}"
            f"（耗时 {elapsed:.2f}s / {source_count:,} 来源）",
        ),
        (
            "扫描",
            f"{_strm_count(stats, 'directories'):,} 目录 · "
            f"{_strm_count(stats, 'scanned_files'):,} 文件",
        ),
        (
            "STRM",
            f"{_strm_count(stats, 'created'):,} 新建 · "
            f"{_strm_count(stats, 'updated'):,} 更新 · "
            f"{_strm_count(stats, 'skipped'):,} 跳过 · "
            f"{_strm_count(stats, 'failed'):,} 失败",
        ),
        (
            "元数据",
            f"{_strm_count(stats, 'metadata_generated'):,} 更新 · "
            f"{_strm_count(stats, 'metadata_queued'):,} 后台排队",
        ),
        ("清理", _strm_cleanup_text(stats)),
        ("媒体库", refresh_text),
    ]
    if source_overview:
        fields.append(("来源概览", source_overview))

    return NotificationEvent(
        "光鸭 STRM 同步全部完成" if not (partial or stopped) else "光鸭 STRM 同步结束",
        layout="compact_report",
        field_emojis=False,
        fields=tuple(fields),
        lines=tuple(lines),
        footer=footer,
    )


def _deliver_strm_terminal(
    chat_id: object,
    event: NotificationEvent,
    progress_handle: TelegramProgress | None = None,
) -> bool:
    """只交付一条 STRM 终态；进度句柄负责失败持久化与重启恢复。"""
    finisher = getattr(progress_handle, "finish", None)
    if callable(finisher):
        return bool(finisher(render_event(event)))
    return send_event_result(event, chat_id=str(chat_id)).ok


def _do_sync(chat_id, progress_handle=None, sync_mode: str = "full"):
    """后台执行 STRM 同步，统一走调度器锁、记录与媒体库刷新。"""
    global _sync_running
    try:
        from app.modules.scheduler import get_scheduler

        progress = _make_strm_progress_editor(
            _bot or get_bot(), str(chat_id), progress_handle
        )
        result = get_scheduler().run_blocking(
            "telegram", on_progress=progress, sync_mode=sync_mode
        )
        if result.get("ok"):
            if progress_handle is not None:
                source_message = getattr(progress_handle, "source_message", None)
                removed = progress_handle.dismiss_source_message()
                if source_message is not None and not removed:
                    _edit_write_confirmation_message(
                        _bot or get_bot(),
                        source_message,
                        "STRM 同步已完成",
                        "确认按钮已失效，结果见下方汇总。",
                    )
            stats = result["stats"]
            source_results = [
                source
                for source in (result.get("sources") or [])
                if isinstance(source, dict)
            ]
            _deliver_strm_terminal(
                chat_id,
                _strm_summary_event(result, stats, len(source_results), source_results),
                progress_handle,
            )
        else:
            source_message = getattr(progress_handle, "source_message", None)
            if source_message is not None:
                _edit_write_confirmation_message(
                    _bot or get_bot(),
                    source_message,
                    "STRM 同步未启动",
                    str(result.get("error") or "请检查配置后重新发起。"),
                )
            _deliver_strm_terminal(
                chat_id,
                NotificationEvent(
                    "STRM 同步失败",
                    layout="relaxed",
                    fields=(("错误原因", result.get("error", "未知错误")),),
                ),
                progress_handle,
            )
    except Exception as e:
        logger.error("STRM 同步失败: %s", e)
        source_message = getattr(progress_handle, "source_message", None)
        if source_message is not None:
            _edit_write_confirmation_message(
                _bot or get_bot(),
                source_message,
                "STRM 同步失败",
                "执行过程中发生异常，请查看日志后重试。",
            )
        _deliver_strm_terminal(
            chat_id,
            NotificationEvent(
                "STRM 同步失败",
                layout="relaxed",
                fields=(("错误原因", e),),
            ),
            progress_handle,
        )
    finally:
        _sync_running = False
        if _task_lock.locked():
            _task_lock.release()


def _organize_terminal_progress(state: dict) -> str:
    status = str(state.get("status") or "failed")
    stats = state.get("stats") if isinstance(state.get("stats"), dict) else {}
    title = {
        "completed": "光鸭整理完成",
        "partial": "光鸭整理部分完成",
        "stopped": "光鸭整理已停止",
        "failed": "光鸭整理失败",
    }.get(status, "光鸭整理已结束")
    counts = (
        f"视频 {int(stats.get('total', 0) or 0)} · "
        f"已移动 {int(stats.get('moved', 0) or 0)} · "
        f"需确认 {int(stats.get('need_confirm', 0) or 0)} · "
        f"失败 {int(stats.get('failed', 0) or 0)}"
    )
    lines = [f"<b>{html.escape(title)}</b>", html.escape(counts)]
    error = str(state.get("error") or "").strip()
    if error:
        lines.append(f"说明：{html.escape(error[:500])}")
    return "\n".join(lines)


def _finish_organize_progress_when_lifecycle_settles(
    chat_id: object,
    state: dict,
    progress: TelegramProgress,
) -> None:
    """保持 typing，直到整理卡片的 STRM/媒体库终态真实送达。"""
    from app.modules.telegram_organize_lifecycle import (
        wait_for_organize_lifecycle_delivery,
    )

    task_id = str(state.get("id") or "").strip()
    delivered = wait_for_organize_lifecycle_delivery(
        task_id,
        chat_id=str(chat_id),
        timeout_seconds=_ORGANIZE_LIFECYCLE_WAIT_SECONDS,
        cancel_event=progress.finished_event,
    )
    if progress.finished_event.is_set():
        return
    if delivered:
        progress.dismiss("光鸭整理已结束。")
        return
    progress.finish(
        "<b>光鸭整理已完成，结果卡仍在收尾</b>\n"
        "STRM 或媒体库状态可能仍在后台更新；请稍后查看结果卡，"
        "也可使用 /status 核对运行状态。"
    )


def _await_organize_lifecycle(
    chat_id: object,
    state: dict,
    progress: TelegramProgress,
) -> None:
    """整理后台线程继续等待完整链路，避免进度与互斥状态过早结束。"""
    progress.update(
        "<b>光鸭整理已完成，正在收尾</b>\n结果卡已生成，正在等待 STRM 与媒体库状态回写…"
    )
    _finish_organize_progress_when_lifecycle_settles(chat_id, state, progress)


def _detach_organize_confirmation(
    progress: TelegramProgress | None,
    *,
    accepted_title: str,
) -> None:
    if progress is None:
        return
    source_message = getattr(progress, "source_message", None)
    dismiss_source = getattr(progress, "dismiss_source_message", None)
    if not callable(dismiss_source):
        return
    removed = dismiss_source()
    if source_message is not None and not removed:
        progress_bot = getattr(progress, "bot", None) or _bot or get_bot()
        _edit_write_confirmation_message(
            progress_bot,
            source_message,
            accepted_title,
            "确认按钮已失效，任务正在后台执行。",
        )


def _run_guangya_organize_stage(
    chat_id: object,
    sources: list[dict[str, str]],
    dst: str,
    progress: TelegramProgress | None,
    *,
    notify_results: bool,
    progress_title: str,
) -> dict:
    from dataclasses import replace

    from app.modules.organize import OrganizeRules
    from app.modules.organize_tasks import get_organize_manager

    manager = get_organize_manager()
    rules = OrganizeRules.from_config(dst)
    if not notify_results:
        rules = replace(
            rules,
            notify_enabled=False,
            library_notify=False,
            strm_detail_notify=False,
        )
    result = manager.start(
        sources,
        rules,
        trigger_type="telegram",
        chat_id=str(chat_id),
    )
    if not result.get("ok"):
        return {
            "status": "failed",
            "error": str(result.get("error") or "光鸭整理未能启动"),
            "notification_sent": False,
            "stats": {},
        }
    if progress is not None:
        progress.bind_task_run("guangya_organize", result.get("run_id"))
    _detach_organize_confirmation(progress, accepted_title="整理任务已接纳")

    task_id = str(result.get("task_id") or "")
    last_state = ""
    while True:
        state = manager.task_result(task_id)
        if state is None:
            state = {
                "id": task_id,
                "status": "failed",
                "error": "整理任务状态不可恢复，请前往整理日志核对结果",
                "notification_sent": False,
                "stats": {},
            }
        status = str(state.get("status") or "")
        current = str(state.get("current_source") or "")
        group_index, group_total, group_name, _stage = _organize_group_progress(state)
        marker = (
            f"{status}:{current}:{group_index}/{group_total}:{group_name}"
            if group_total
            else f"{status}:{current}:{state.get('message', '')}"
        )
        if (
            progress is not None
            and marker != last_state
            and status in {"running", "stopping"}
        ):
            current_line = f"\n当前：{html.escape(current)}" if current else ""
            group_line = ""
            if group_total:
                group_line = f"\n媒体目录：{group_index}/{group_total}"
                if group_name:
                    group_line += f" · {html.escape(group_name)}"
            progress.update(
                f"<b>{html.escape(progress_title)}</b>\n"
                f"任务：{html.escape(task_id)}\n"
                f"范围：{len(sources)} 个来源目录{current_line}{group_line}"
            )
            last_state = marker
        if status not in {"running", "stopping"}:
            return state
        time.sleep(1.0)


def _run_local_organize_stage(
    progress: TelegramProgress | None,
    *,
    progress_title: str,
) -> dict:
    from app.modules.local_media_scheduler import get_local_media_scheduler

    scheduler = get_local_media_scheduler()
    was_running = bool(scheduler.status().get("running"))
    scan_result = scheduler.enqueue_manual_scan_candidates(
        silent=True,
        capture_results=True,
    )
    if scan_result.get("task_ids") and not was_running:
        scheduler.start()
    _detach_organize_confirmation(progress, accepted_title="本地整理任务已接纳")

    task_ids = [int(item) for item in (scan_result.get("task_ids") or [])]
    last_marker = ""
    while task_ids:
        tasks = [
            db.get_local_media_task(task_id, owner="admin") for task_id in task_ids
        ]
        statuses = [
            str(task.status) if task is not None else "failed" for task in tasks
        ]
        active = sum(
            status not in {"completed", "failed", "requires_manual"}
            for status in statuses
        )
        marker = ":".join(statuses)
        if progress is not None and marker != last_marker:
            completed = statuses.count("completed")
            review = statuses.count("requires_manual")
            failed = statuses.count("failed")
            progress.update(
                f"<b>{html.escape(progress_title)}</b>\n"
                f"候选：{len(task_ids)} 个\n"
                f"进度：{completed + review + failed}/{len(task_ids)} 完成"
                f" · {review} 待确认 · {failed} 失败"
            )
            last_marker = marker
        if not active:
            break
        time.sleep(1.0)

    completed = 0
    requires_manual = 0
    failed = 0
    moved_items = 0
    skipped_items = 0
    task_results: dict[int, dict] = {}
    take_result = getattr(scheduler, "take_captured_task_result", None)
    for task_id in task_ids:
        task = db.get_local_media_task(task_id, owner="admin")
        status = str(getattr(task, "status", "failed"))
        completed += int(status == "completed")
        requires_manual += int(status == "requires_manual")
        failed += int(status == "failed")
        captured_result = take_result(task_id) if callable(take_result) else None
        if status == "requires_manual":
            task_results[task_id] = (
                captured_result
                if isinstance(captured_result, dict)
                else {"status": "requires_manual"}
            )
        if status == "completed":
            for item in db.list_local_media_task_items(task_id, owner="admin"):
                if str(item["action"] or "") == "skip":
                    skipped_items += 1
                else:
                    moved_items += 1
    source_errors = [
        f"{item.get('name') or '未命名来源'!s}：{item.get('error') or ''!s}"
        for item in (scan_result.get("sources") or [])
        if isinstance(item, dict) and str(item.get("error") or "")
    ]
    return {
        **scan_result,
        "completed": completed,
        "requires_manual": requires_manual,
        "failed": failed,
        "moved_items": moved_items,
        "skipped_items": skipped_items,
        "source_errors": source_errors,
        "task_results": task_results,
    }


def _local_organize_event(summary: dict) -> NotificationEvent:
    attention = bool(
        int(summary.get("requires_manual", 0) or 0)
        or int(summary.get("failed", 0) or 0)
        or summary.get("source_errors")
    )
    lines = [f"• {message}" for message in list(summary.get("source_errors") or [])[:5]]
    if not int(summary.get("candidate_count", 0) or 0):
        lines.append("下载目录中暂未发现可整理的媒体候选。")
    return NotificationEvent(
        "本地下载整理部分完成" if attention else "本地下载整理完成",
        layout="relaxed",
        fields=(
            ("状态", "需要处理" if attention else "整理完成"),
            (
                "来源",
                f"{int(summary.get('scanned_sources', 0) or 0):,} 已扫描 · "
                f"{int(summary.get('source_count', 0) or 0):,} 已配置",
            ),
            ("候选", f"{int(summary.get('candidate_count', 0) or 0):,} 个"),
            (
                "任务",
                f"{int(summary.get('completed', 0) or 0):,} 完成 · "
                f"{int(summary.get('requires_manual', 0) or 0):,} 待确认 · "
                f"{int(summary.get('failed', 0) or 0):,} 失败",
            ),
            (
                "文件",
                f"{int(summary.get('moved_items', 0) or 0):,} 已归档 · "
                f"{int(summary.get('skipped_items', 0) or 0):,} 按冲突策略跳过",
            ),
        ),
        lines=tuple(lines),
        footer=(
            "本地待确认项目将继续发送候选卡；若未收到，"
            "请前往 Web「本地媒体 → 待确认」处理。"
            if int(summary.get("requires_manual", 0) or 0)
            else ""
        ),
    )


def _all_organize_event(cloud_state: dict, local_summary: dict) -> NotificationEvent:
    cloud_status = str(cloud_state.get("status") or "failed")
    cloud_stats = (
        cloud_state.get("stats") if isinstance(cloud_state.get("stats"), dict) else {}
    )
    cloud_need_confirm = int(cloud_stats.get("need_confirm", 0) or 0)
    cloud_failed = int(cloud_stats.get("failed", 0) or 0)
    cloud_skipped = int(cloud_stats.get("skipped", 0) or 0)
    cloud_scan_incomplete = bool(
        cloud_stats.get("scan_errors")
        or cloud_stats.get("scan_limited")
        or cloud_stats.get("scan_complete") is False
    )
    local_attention = bool(
        int(local_summary.get("requires_manual", 0) or 0)
        or int(local_summary.get("failed", 0) or 0)
        or local_summary.get("source_errors")
    )
    lines: list[str] = []
    cloud_error = str(cloud_state.get("error") or "").strip()
    if cloud_error:
        lines.append(f"光鸭：{cloud_error[:300]}")
    lines.extend(
        f"本地：{message}"
        for message in list(local_summary.get("source_errors") or [])[:4]
    )
    strm = cloud_stats.get("strm") if isinstance(cloud_stats.get("strm"), dict) else {}
    strm_text = "未触发"
    if strm:
        strm_text = (
            "已触发" if strm.get("ok") else str(strm.get("error") or "未完成")[:120]
        )
    strm_attention = bool(strm and not strm.get("ok") and not strm.get("skipped"))
    attention = bool(
        cloud_status != "completed"
        or cloud_need_confirm
        or cloud_failed
        or cloud_skipped
        or cloud_scan_incomplete
        or strm_attention
        or local_attention
    )
    footer = "全部整理按“光鸭云盘 → 本地下载”顺序执行。"
    pending_sources: list[str] = []
    if cloud_need_confirm:
        pending_sources.append("光鸭")
    if int(local_summary.get("requires_manual", 0) or 0):
        pending_sources.append("本地")
    if pending_sources:
        footer += (
            f"\n\n{'、'.join(pending_sources)}待确认项目将继续发送候选卡；"
            "若未收到，请前往 Web 待确认队列继续处理。"
        )
    return NotificationEvent(
        "全部整理部分完成" if attention else "全部整理完成",
        layout="relaxed",
        fields=(
            ("状态", "需要处理" if attention else "全部完成"),
            (
                "光鸭云盘",
                f"{int(cloud_stats.get('moved', 0) or 0):,} 已移动 · "
                f"{int(cloud_stats.get('need_confirm', 0) or 0):,} 待确认 · "
                f"{int(cloud_stats.get('failed', 0) or 0):,} 失败",
            ),
            ("STRM", strm_text),
            (
                "本地下载",
                f"{int(local_summary.get('completed', 0) or 0):,} 完成 · "
                f"{int(local_summary.get('requires_manual', 0) or 0):,} 待确认 · "
                f"{int(local_summary.get('failed', 0) or 0):,} 失败",
            ),
            ("本地归档", f"{int(local_summary.get('moved_items', 0) or 0):,} 个文件"),
        ),
        lines=tuple(lines),
        footer=footer,
    )


def _notify_local_organize_confirmations(summary: dict, chat_id: object) -> int:
    """只发送本地 requires_manual 候选卡，保持批量完成通知静音。"""
    from app.modules.local_media_notifications import notify_local_media_task

    raw_results = summary.get("task_results")
    task_results = raw_results if isinstance(raw_results, dict) else {}
    delivered = 0
    seen: set[int] = set()
    for raw_task_id in summary.get("task_ids") or []:
        try:
            task_id = int(raw_task_id)
        except (TypeError, ValueError):
            continue
        if task_id <= 0 or task_id in seen:
            continue
        seen.add(task_id)
        task = db.get_local_media_task(task_id, owner="admin")
        if str(getattr(task, "status", "")) != "requires_manual":
            continue
        result = task_results.get(task_id, task_results.get(str(task_id)))
        if not isinstance(result, dict):
            result = {"status": "requires_manual"}
        try:
            delivered += int(
                bool(
                    notify_local_media_task(
                        task_id,
                        result,
                        owner="admin",
                        chat_id=str(chat_id),
                    )
                )
            )
        except Exception as exc:
            logger.warning(
                "Telegram 本地整理待确认通知失败 task=%s type=%s",
                task_id,
                type(exc).__name__,
            )
    return delivered


def _do_organize(
    chat_id,
    sources: list[dict[str, str]],
    dst: str,
    progress: TelegramProgress | None = None,
):
    """后台执行光鸭多源整理；业务终态后不再占用互斥锁等待 TG 回执。"""
    global _organize_running
    slot_released = False

    def release_maintenance_slot() -> None:
        nonlocal slot_released
        global _organize_running
        if slot_released:
            return
        _organize_running = False
        if _task_lock.locked():
            _task_lock.release()
        slot_released = True

    try:
        state = _run_guangya_organize_stage(
            chat_id,
            sources,
            dst,
            progress,
            notify_results=True,
            progress_title="光鸭整理进行中",
        )
        # STRM/媒体库结果卡的投递确认最长可等待 30 分钟，但它已不是整理
        # 写操作的一部分。先释放业务互斥槽，避免 Telegram 网络状态阻塞后续任务。
        release_maintenance_slot()
        if progress is not None:
            if state.get("notification_sent"):
                _await_organize_lifecycle(chat_id, state, progress)
            else:
                progress.finish(_organize_terminal_progress(state))
        elif str(state.get("status") or "") == "failed":
            send_event_result(
                NotificationEvent(
                    "整理失败",
                    fields=(
                        (
                            "错误原因",
                            str(state.get("error") or "任务未能正常启动"),
                        ),
                    ),
                    layout="relaxed",
                ),
                chat_id=str(chat_id),
            )
    except Exception as exc:
        logger.error("Telegram 整理启动失败 type=%s", type(exc).__name__)
        if progress is not None:
            progress.finish("<b>光鸭整理失败</b>\n任务未能正常启动，请查看日志后重试。")
        else:
            send_event_result(
                NotificationEvent(
                    "整理失败",
                    fields=(("错误原因", "任务未能正常启动"),),
                    layout="relaxed",
                ),
                chat_id=str(chat_id),
            )
    finally:
        release_maintenance_slot()


def _do_organize_local(chat_id, progress: TelegramProgress | None = None) -> None:
    global _local_organize_running
    try:
        summary = _run_local_organize_stage(
            progress,
            progress_title="本地下载整理进行中",
        )
        event = _local_organize_event(summary)
        if progress is not None:
            progress.finish(render_event(event))
        else:
            send_event_result(event, chat_id=str(chat_id))
        _notify_local_organize_confirmations(summary, str(chat_id))
    except Exception as exc:
        logger.error("Telegram 本地整理失败 type=%s", type(exc).__name__)
        if progress is not None:
            progress.finish("<b>本地下载整理失败</b>\n任务执行异常，请查看日志后重试。")
        else:
            send_event_result(
                NotificationEvent(
                    "本地下载整理失败",
                    fields=(("错误原因", "任务执行异常，请查看日志后重试"),),
                    layout="relaxed",
                ),
                chat_id=str(chat_id),
            )
    finally:
        _local_organize_running = False
        if _task_lock.locked():
            _task_lock.release()


def _do_organize_all(
    chat_id,
    sources: list[dict[str, str]],
    dst: str,
    progress: TelegramProgress | None = None,
) -> None:
    global _organize_running, _local_organize_running
    try:
        cloud_state = _run_guangya_organize_stage(
            chat_id,
            sources,
            dst,
            progress,
            notify_results=False,
            progress_title="全部整理 · 阶段 1/2 · 光鸭云盘",
        )
        if progress is not None:
            progress.update(
                "<b>全部整理 · 阶段 2/2 · 本地下载</b>\n"
                "正在扫描已有媒体并创建本地归档任务…"
            )
        local_summary = _run_local_organize_stage(
            progress,
            progress_title="全部整理 · 阶段 2/2 · 本地下载",
        )
        event = _all_organize_event(cloud_state, local_summary)
        if progress is not None:
            progress.finish(render_event(event))
        else:
            send_event_result(event, chat_id=str(chat_id))
        cloud_stats = (
            cloud_state.get("stats")
            if isinstance(cloud_state.get("stats"), dict)
            else {}
        )
        if cloud_stats.get("confirmation_groups"):
            try:
                from app.modules.organize import Organizer, OrganizeRules

                Organizer.notify_task_confirmations(
                    cloud_stats,
                    OrganizeRules.from_config(dst),
                    source_name=f"{len(sources)} 个源目录",
                    chat_id=str(chat_id),
                )
            except Exception as exc:
                logger.warning(
                    "Telegram 全部整理待确认通知失败 type=%s",
                    type(exc).__name__,
                )
        _notify_local_organize_confirmations(local_summary, str(chat_id))
    except Exception as exc:
        logger.error("Telegram 全部整理失败 type=%s", type(exc).__name__)
        if progress is not None:
            progress.finish("<b>全部整理失败</b>\n任务执行异常，请查看日志后重试。")
        else:
            send_event_result(
                NotificationEvent(
                    "全部整理失败",
                    fields=(("错误原因", "任务执行异常，请查看日志后重试"),),
                    layout="relaxed",
                ),
                chat_id=str(chat_id),
            )
    finally:
        _organize_running = False
        _local_organize_running = False
        if _task_lock.locked():
            _task_lock.release()


def start_bot_blocking(stop_event: threading.Event | None = None):
    """阻塞式启动共享 Bot polling（在单独线程调用）。"""
    if stop_event is not None and stop_event.is_set():
        return
    bot = init_bot(stop_event=stop_event)
    if bot is None or (stop_event is not None and stop_event.is_set()):
        return
    delay = 1.0
    effective_stop = stop_event or threading.Event()
    while not effective_stop.is_set():
        _ensure_command_menu(bot)
        logger.debug("TG Bot 启动 polling")
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=30)
            if stop_event is None or not stop_event.is_set():
                logger.warning("TG Bot polling 意外结束，准备自动重连")
        except Exception as exc:
            log_throttled(
                logger,
                logging.ERROR,
                f"tg-bot-polling:{type(exc).__name__}",
                "TG Bot polling 异常退出 type=%s，将自动重连",
                type(exc).__name__,
            )
        if effective_stop.wait(delay):
            break
        delay = min(30.0, delay * 2)


def _start_bot_locked() -> bool:
    """在生命周期控制锁内启动 polling 线程。"""
    global _bot_thread, _bot_thread_stop
    with _lifecycle_lock:
        if _bot_thread and _bot_thread.is_alive():
            return False
        if not _configuration_complete():
            return False
        stop_event = threading.Event()
        thread = threading.Thread(
            target=start_bot_blocking,
            args=(stop_event,),
            name="telegram-bot",
            daemon=True,
        )
        _bot_thread_stop = stop_event
        _bot_thread = thread
        try:
            thread.start()
        except Exception:
            if _bot_thread is thread:
                _bot_thread = None
            if _bot_thread_stop is stop_event:
                _bot_thread_stop = None
            raise
        return True


def start_bot() -> bool:
    """幂等启动 polling 线程，供 lifespan 和配置热更新调用。"""
    with _lifecycle_control_lock:
        return _start_bot_locked()


def _stop_bot_locked(timeout: float = 5.0, *, cancel_operations: bool = True) -> bool:
    """在生命周期控制锁内停止 polling 与恢复线程。"""
    global _bot, _bot_thread, _bot_thread_stop, _registered_bot_id
    global _progress_recovery_thread, _progress_recovery_stop
    with _lifecycle_lock:
        bot = _bot
        thread = _bot_thread
        thread_stop = _bot_thread_stop
        recovery_thread = _progress_recovery_thread
        recovery_stop = _progress_recovery_stop
    if thread_stop is not None:
        thread_stop.set()
    if recovery_stop is not None:
        recovery_stop.set()
    if bot is not None:
        try:
            if cancel_operations:
                from app.bot.progress import cancel_active_operations

                cancel_active_operations(
                    "服务正在停止，本次操作已结束；启动完成后可重新发起。"
                )
        except Exception as exc:
            logger.warning("收尾 Telegram 长任务失败 type=%s", type(exc).__name__)
    try:
        from app.bot.progress import stop_terminal_delivery_retries

        # 先让运行中的操作持久化终态，再停止本代重投；下次 start 会创建新代恢复。
        stop_terminal_delivery_retries(timeout=timeout)
    except Exception as exc:
        logger.warning("停止 Telegram 终态重投失败 type=%s", type(exc).__name__)
    if bot is not None:
        try:
            bot.stop_polling()
            logger.info("TG Bot polling 已停止")
        except Exception as exc:
            logger.warning("停止 TG Bot polling 失败 type=%s", type(exc).__name__)
    if thread and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=timeout)
    if (
        recovery_thread
        and recovery_thread.is_alive()
        and recovery_thread is not threading.current_thread()
    ):
        recovery_thread.join(timeout=timeout)
    with _lifecycle_lock:
        thread_finished = not thread or not thread.is_alive()
        recovery_finished = not recovery_thread or not recovery_thread.is_alive()
        if thread is None:
            if _bot is bot:
                _bot = None
        elif _bot_thread is thread and thread_finished:
            _bot_thread = None
            if _bot_thread_stop is thread_stop:
                _bot_thread_stop = None
            # 只有当前代 polling 已退出时，才清理它可能在 stop 快照后设置的 bot。
            _bot = None
        if _progress_recovery_thread is recovery_thread and recovery_finished:
            _progress_recovery_thread = None
        if _progress_recovery_stop is recovery_stop and recovery_finished:
            _progress_recovery_stop = None
        if thread_finished and recovery_finished:
            # notifier 可能复用同一个 TeleBot 对象；每次 polling 新代都必须
            # 重新创建终态重投/恢复 generation，不能只依赖对象 identity。
            _registered_bot_id = None
    try:
        from app.modules.telegram_resource_search import (
            shutdown_telegram_indexer_worker,
        )

        shutdown_telegram_indexer_worker(timeout=timeout)
    except Exception as exc:
        logger.warning("停止 Telegram 资源站服务失败 type=%s", type(exc).__name__)
    return thread_finished and recovery_finished


def stop_bot(timeout: float = 5.0) -> bool:
    """停止 Telegram 长轮询，并返回 polling 与恢复线程是否均已退出。"""
    with _lifecycle_control_lock:
        return _stop_bot_locked(timeout=timeout)


def restart_bot() -> bool:
    """配置更新后原子地停止旧 polling、重置客户端并启动新代。"""
    global _registered_bot_id, _command_menu_bot_id
    from app.notifier import reset

    with _lifecycle_control_lock:
        if not _stop_bot_locked(cancel_operations=False):
            logger.error("Telegram Bot 旧 polling 未在超时内退出，已取消热重启")
            return False
        reset()
        with _lifecycle_lock:
            _registered_bot_id = None
            _command_menu_bot_id = None
        return _start_bot_locked()
