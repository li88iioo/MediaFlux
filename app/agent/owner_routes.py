"""Agent owner 的低层、安全通知路由解析。"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from app import config
from app import database as db

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_TELEGRAM_OWNER_RE = re.compile(r"^tg:v1:(-?[1-9][0-9]*)\x1f([1-9][0-9]*)$")

_TELEGRAM_CHAT_RE = re.compile(r"^-?[1-9][0-9]*$")
_TELEGRAM_THREAD_RE = re.compile(r"^[1-9][0-9]{0,18}$")


class TransportInputError(ValueError):
    """对外安全的入口参数错误。"""


def normalize_delivery_route(
    value: Mapping[str, Any], *, channel: str = "telegram", owner: str = "",
) -> dict[str, Any]:
    """校验服务端物理投递地址；HTTP 和模型上下文不是地址来源。"""
    if not isinstance(value, Mapping):
        raise TransportInputError("delivery_route 无效")
    if not value:
        return {}
    if channel != "telegram" or set(value) - {"chat_id", "message_thread_id"}:
        raise TransportInputError("delivery_route 仅允许 Telegram 服务端设置")
    chat = value.get("chat_id")
    if isinstance(chat, bool) or not _TELEGRAM_CHAT_RE.fullmatch(str(chat)):
        raise TransportInputError("delivery_route.chat_id 无效")
    target = str(int(str(chat)))
    if owner:
        identity = _TELEGRAM_OWNER_RE.fullmatch(owner)
        if identity is None or identity.group(1) != target:
            raise TransportInputError("delivery_route 不属于当前 Telegram owner")
    route: dict[str, Any] = {"chat_id": target}
    thread = value.get("message_thread_id")
    if thread is not None:
        if isinstance(thread, bool) or not _TELEGRAM_THREAD_RE.fullmatch(str(thread)):
            raise TransportInputError("delivery_route.message_thread_id 无效")
        route["message_thread_id"] = int(thread)
    return route

def web_kernel_owner(login_identity: object) -> str:
    """由已认证登录身份派生跨浏览器会话稳定、不可逆的 Web principal。"""
    normalized_owner = str(login_identity or "").strip()
    if not normalized_owner or len(normalized_owner) > 512:
        raise ValueError("Web Agent owner 无效")
    payload = b"mediaflux-agent-web-kernel:v1\0" + normalized_owner.encode("utf-8")
    return f"webk:v1:{hashlib.sha256(payload).hexdigest()}"


@dataclass(frozen=True)
class TelegramOwnerRoute:
    owner: str
    chat_id: str
    user_id: str


def parse_telegram_owner_route(value: object) -> TelegramOwnerRoute | None:
    """仅接受规范 Telegram owner；其它 owner 不得回退到全局 chat。"""
    owner = str(value or "")
    match = _TELEGRAM_OWNER_RE.fullmatch(owner)
    if match is None:
        return None
    chat_id, user_id = match.groups()
    return TelegramOwnerRoute(owner=owner, chat_id=chat_id, user_id=user_id)


def configured_telegram_user_ids() -> set[str]:
    """TG 输入、Kernel 与历史回执共用的实时白名单语法。"""
    return {
        part
        for part in re.split(
            r"[,;，；\s]+",
            (config.get("TG_AGENT_ALLOWED_USER_IDS", "") or "").strip(),
        )
        if re.fullmatch(r"[1-9][0-9]*", part)
    }


def telegram_channel_owners() -> list[str]:
    """Web 只读渠道可见的 Telegram Kernel 身份：与 TG 输入授权同一白名单。"""
    chat = (config.get("TG_CHAT_ID", "") or "").strip()
    if not _TELEGRAM_CHAT_RE.fullmatch(chat):
        return []
    return [f"tg:v1:{chat}\x1f{user}" for user in sorted(configured_telegram_user_ids())]


def telegram_owner_route_access(
    value: object,
    *,
    chat_id: object = "",
    require_enabled: bool = True,
) -> Literal["allowed", "unauthorized", "disabled"]:
    """输入、工具与回执共用实时授权；开关控制可在 Agent 关闭时使用。"""
    route = (
        value
        if isinstance(value, TelegramOwnerRoute)
        else parse_telegram_owner_route(value)
    )
    if route is None:
        return "unauthorized"
    persisted_chat = str(chat_id or "").strip()
    if persisted_chat and persisted_chat != route.chat_id:
        return "unauthorized"
    enabled = not require_enabled or (
        config.get_bool("AGENT_ENABLED", False)
        and config.get_bool("TG_AGENT_ENABLED", False)
    )
    if ((config.get("TG_CHAT_ID", "") or "").strip() != route.chat_id
            or route.user_id not in configured_telegram_user_ids()):
        return "unauthorized"
    return "allowed" if enabled else "disabled"


def telegram_owner_route_is_currently_authorized(
    value: object,
    *,
    chat_id: object = "",
) -> bool:
    """按当前 Agent/TG 配置重新授权持久化 owner，撤权后立即失效。"""
    return telegram_owner_route_access(value, chat_id=chat_id) == "allowed"


_MAIN_SESSION_KEY = "agent.web.main_session.v1:"


def _main_session_key(owner: str) -> str:
    return _MAIN_SESSION_KEY + hashlib.sha256(owner.encode("utf-8")).hexdigest()[:32]


def current_web_main_session_id(owner: str) -> str:
    """只读当前“主要聊天”会话；尚未分配时返回空串。"""
    value = db.kv_get(_main_session_key(owner))
    return value if SESSION_ID_RE.fullmatch(value) else ""


def web_main_session_id(owner: str, sessions: list[dict[str, Any]]) -> str:
    """固定的“主要聊天”：首次分配时沿用最近一次会话，没有会话则新建，之后不随新会话改变。"""
    key = _main_session_key(owner)
    with db.get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT value FROM settings_kv WHERE key=?", (key,)).fetchone()
        if row is not None and SESSION_ID_RE.fullmatch(str(row["value"])):
            return str(row["value"])
        candidates = [item for item in sessions if SESSION_ID_RE.fullmatch(str(item.get("session_id") or ""))]
        newest = max(candidates, key=lambda item: float(item.get("updated_at") or 0), default=None)
        value = str(newest["session_id"]) if newest else f"session_{secrets.token_hex(12)}"
        db.kv_set_conn(conn, key, value)
        return value
