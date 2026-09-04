"""光鸭账号容量与连接资料的最小公开投影。"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from app.agent.errors import AgentToolError
from app.agent.models import Evidence, ToolResult
from app.agent.public_safety import sanitize_public_text
from app.clients.guangya import GuangYaClient

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def guangya_account_status_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or arguments:
        raise AgentToolError("光鸭账号状态查询不接受参数")
    return {}


def _payloads(value: object) -> Iterable[dict[str, Any]]:
    if not isinstance(value, dict):
        return ()
    rows = [value]
    for key in ("data", "user", "userInfo", "user_info", "storage", "space"):
        nested = value.get(key)
        if isinstance(nested, dict):
            rows.append(nested)
            for child_key in ("user", "userInfo", "storage", "space"):
                child = nested.get(child_key)
                if isinstance(child, dict):
                    rows.append(child)
    return rows


def _first(payloads: Iterable[dict[str, Any]], *keys: str) -> object:
    for payload in payloads:
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return value
    return ""


def _bytes(payloads: list[dict[str, Any]], *keys: str) -> int | None:
    value = _first(payloads, *keys)
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        parsed = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    return max(0, parsed)


def _mask_phone(value: object) -> str:
    raw = "".join(character for character in str(value or "") if character.isdigit())
    if len(raw) < 7:
        return ""
    return raw[:3] + "****" + raw[-4:]


def _mask_email(value: object) -> str:
    text = str(value or "").strip()
    local, separator, domain = text.partition("@")
    if not separator or not local or not domain:
        return ""
    return local[:1] + "***@" + domain[:120]


def get_guangya_account_status(_arguments: dict[str, Any]) -> ToolResult:
    client: GuangYaClient | None = None
    try:
        client = GuangYaClient()
        if not client.logged_in:
            raise AgentToolError("光鸭账号尚未连接", code="precondition_failed")
        raw = client.account_info()
        payloads = list(_payloads(raw))
    except AgentToolError:
        raise
    except Exception as exc:
        logger.warning("Agent 光鸭账号资料读取失败 type=%s", type(exc).__name__)
        raise AgentToolError("光鸭账号状态当前不可用", code="unavailable") from exc
    finally:
        if client is not None:
            client.close()

    display_name = sanitize_public_text(
        _first(payloads, "nickname", "nickName", "username", "userName", "name"),
        limit=80,
    )
    phone = _mask_phone(
        _first(payloads, "phone", "phoneNumber", "mobile", "mobilePhone")
    )
    email = _mask_email(_first(payloads, "email", "emailAddress"))
    total = _bytes(
        payloads,
        "totalSpace",
        "totalSize",
        "storageTotal",
        "total_capacity",
        "capacity",
        "quota",
    )
    used = _bytes(
        payloads,
        "usedSpace",
        "usedSize",
        "storageUsed",
        "used_capacity",
        "useSpace",
    )
    available = _bytes(
        payloads,
        "availableSpace",
        "freeSpace",
        "storageFree",
        "available_capacity",
        "remainSpace",
    )
    if available is None and total is not None and used is not None:
        available = max(0, total - used)
    if used is None and total is not None and available is not None:
        used = max(0, total - available)
    utilization = (
        round(min(1.0, used / total), 4)
        if total and used is not None
        else None
    )
    data = {
        "connected": True,
        "display_name": display_name,
        "masked_phone": phone,
        "masked_email": email,
        "storage": {
            "total_bytes": total,
            "used_bytes": used,
            "available_bytes": available,
            "utilization": utilization,
            "reported": any(value is not None for value in (total, used, available)),
        },
    }
    return ToolResult(
        True,
        "ok",
        (
            "光鸭账号已连接，并已读取容量信息"
            if data["storage"]["reported"]
            else "光鸭账号已连接，服务端本次未返回容量字段"
        ),
        data=data,
        model_data={
            "connected": True,
            "storage": data["storage"],
            "identity_available": bool(display_name or phone or email),
        },
        evidence=[
            Evidence(
                "guangya_account",
                "仅投影账号显示名、掩码联系方式和容量白名单字段；未返回用户 ID、Token 或原始响应。",
                _now(),
            )
        ],
    )
