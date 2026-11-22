"""光鸭账号、容量和权益的只读查询，保留各来源的部分成功。"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime
from typing import Any

import httpx

from app.clients.guangya import GuangYaClient, close_guangya_client
from app.sensitive_data import redact_sensitive_text

logger = logging.getLogger(__name__)

_MEMBERSHIP_STATES = {1: "none", 2: "active", 3: "expired"}


def connection_status() -> dict[str, Any]:
    """最小只读请求核验连接；检查失败保留未知，不冒充未配置。"""
    client = None
    configured = reachable = None
    try:
        client = GuangYaClient()
        configured = client.logged_in
        reachable = client.probe_connection() if configured else False
        status = "ready" if reachable else "attention" if configured else "not_configured"
    except Exception as exc:
        logger.warning("光鸭连接检查失败 type=%s", type(exc).__name__)
        status = "unavailable"
    finally:
        close_guangya_client(client)
    return {"ok": status == "ready", "status": status,
            "configured": configured, "connected": reachable, "reachable": reachable,
            "sampled_at": datetime.now().astimezone().isoformat(timespec="seconds")}


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
    for payload in payloads:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                continue
            # 字节数必须是非负整数；字符串直接转 int，避免 float 丢失大整数精度。
            if isinstance(value, float) and not value.is_integer():
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError, OverflowError):
                continue
            # 只接受有意义的 64 位字节计数，异常巨数不能让利用率计算溢出。
            if 0 <= parsed <= (1 << 63) - 1:
                return parsed
    return None


def _reported(fields: dict[str, Any], *, available: bool) -> dict[str, Any]:
    known = sum(value is not None for value in fields.values())
    if not available:
        status = "unavailable"
    elif not known:
        status = "not_reported"
    else:
        status = "ok" if known == len(fields) else "partial"
    return {**fields, "reported": bool(known), "status": status}


def _remaining(total: int | None, used: int | None) -> int | None:
    return max(0, total - used) if total is not None and used is not None else None


def _entitlements(assets: dict | None, rights: dict | None) -> dict[str, Any]:
    asset = assets.get("data", {}) if assets is not None else {}
    rights_data = rights.get("data", {}) if rights is not None else {}
    sources = [asset]
    available = assets is not None

    vip = _bytes(sources, "vipStatus")
    svip = _bytes(sources, "svipStatus")
    expires_at = _bytes(sources, "vipExpireTime")
    server_time = _bytes(sources, "systemTime")
    upload_total = _bytes(sources, "monthNonFlashUploadLimitSize")
    upload_used = _bytes(sources, "monthNonFlashUploadSize")
    traffic = asset.get("highSpeedTraffic")
    traffic = traffic if isinstance(traffic, dict) else {}
    details = traffic.get("details")
    speed = _bytes([rights_data], "playbackSpeed")
    return {
        "membership": {
            **_reported({"vip_status": vip, "svip_status": svip, "vip_expires_at": expires_at}, available=available),
            "vip_status_text": _MEMBERSHIP_STATES.get(vip or 0, "unknown"),
            "svip_status_text": _MEMBERSHIP_STATES.get(svip or 0, "unknown"),
            "server_time": server_time,
            "vip_remaining_seconds": _remaining(expires_at, server_time),
        },
        "upload_traffic": {
            **_reported({"total_bytes": upload_total, "used_bytes": upload_used}, available=available),
            "remaining_bytes": _remaining(upload_total, upload_used),
        },
        "high_speed_download_traffic": _reported({
            "total_bytes": _bytes([traffic], "total"),
            "remaining_bytes": _bytes([traffic], "remained"),
            "details": [
                {"platform": _bytes([entry], "platform"), "used_bytes": _bytes([entry], "used")}
                for entry in details if isinstance(entry, dict)
            ] if isinstance(details, list) else None,
        }, available=available),
        "direct_link_traffic": _reported({
            "total_bytes": _bytes(sources, "totalDirectLinkTraffic"),
            "remaining_bytes": _bytes(sources, "freeDirectLinkTraffic"),
            "expires_at": _bytes(sources, "directLinkTrafficExpireTime"),
        }, available=available),
        "share_guest_traffic": _reported({
            "total_bytes": _bytes(sources, "totalShareGuestTraffic"),
            "remaining_bytes": _bytes(sources, "freeShareGuestTraffic"),
        }, available=available),
        "capabilities": {
            **_reported({
                "batch_upload_files": _bytes([rights_data], "batchUploadFiles"),
                "big_file_size_bytes": _bytes([rights_data], "bigFileSize"),
                "playback_speed": speed,
                "direct_link": rights_data.get("directLinkFeature") if isinstance(rights_data.get("directLinkFeature"), bool) else None,
                "share_code": rights_data.get("shareCodeFeature") if isinstance(rights_data.get("shareCodeFeature"), bool) else None,
            }, available=rights is not None),
            "playback_speed_rate": speed / 100 if speed is not None else None,
        },
    }


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


def account_status() -> dict[str, Any]:
    client: GuangYaClient | None = None
    snapshots: dict[str, dict] = {}
    try:
        try:
            client = GuangYaClient()
            connected = client.logged_in
        except Exception as exc:
            logger.warning("光鸭账号连接读取失败 type=%s", type(exc).__name__)
            raise RuntimeError("光鸭账号状态当前不可用") from exc
        if not connected:
            raise RuntimeError("光鸭账号尚未连接")
        for source, read in (
            ("assets", client.account_storage_info),
            ("profile", client.account_info),
            ("rights", client.account_rights_info),
        ):
            try:
                snapshots[source] = read()
            except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
                logger.warning("光鸭账号读取失败 source=%s type=%s", source, type(exc).__name__)
        if not snapshots:
            raise RuntimeError("光鸭账号信息读取失败，当前连接状态无法确认")
    finally:
        close_guangya_client(client)

    payloads = list(_payloads(snapshots.get("profile")))
    storage_payloads = list(_payloads(snapshots.get("assets")))
    display_name = " ".join(redact_sensitive_text(
        _first(payloads, "nickname", "nickName", "username", "userName", "name"),
    ).split())[:80]
    phone = _mask_phone(
        _first(payloads, "phone_number", "phone", "phoneNumber", "mobile", "mobilePhone")
    )
    email = _mask_email(_first(payloads, "email", "emailAddress"))
    total = _bytes(
        storage_payloads,
        "totalSpaceSize",
        "totalSpace",
        "totalSize",
        "storageTotal",
        "total_capacity",
        "capacity",
        "quota",
    )
    used = _bytes(
        storage_payloads,
        "usedSpaceSize",
        "usedSpace",
        "usedSize",
        "storageUsed",
        "used_capacity",
        "useSpace",
    )
    available = _bytes(
        storage_payloads,
        "availableSpace",
        "freeSpace",
        "storageFree",
        "available_capacity",
        "remainSpace",
    )
    if available is None:
        available = _remaining(total, used)
    if used is None and total is not None and available is not None and available <= total:
        used = total - available
    utilization = (
        round(used / total, 4)
        if total and used is not None
        else None
    )
    storage = _reported({"total_bytes": total, "used_bytes": used, "available_bytes": available},
                        available="assets" in snapshots)
    storage["utilization"] = utilization
    if storage["status"] == "unavailable":
        summary = "光鸭账号已连接，但容量接口读取失败，暂时无法确认剩余空间"
    elif storage["status"] == "not_reported":
        summary = "光鸭容量接口已响应，但本次未取得有效容量字段"
    else:
        summary = "光鸭账号已连接，并已读取容量信息" if storage["status"] == "ok" else "光鸭账号已连接，仅取得部分容量信息"
    for source, label in (("profile", "账号资料"), ("rights", "权益接口")):
        if source not in snapshots:
            summary += f"，{label}读取失败"
    return {
        "connected": True,
        "profile_available": "profile" in snapshots,
        "display_name": display_name,
        "masked_phone": phone,
        "masked_email": email,
        "storage": storage,
        **_entitlements(snapshots.get("assets"), snapshots.get("rights")),
        "status": "ok" if len(snapshots) == 3 and storage["status"] == "ok" else "partial",
        "summary": summary,
        "sampled_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
