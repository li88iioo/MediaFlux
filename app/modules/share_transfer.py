"""光鸭分享预览、选择状态与幂等转存服务。

分享访问令牌和签名 URL 只存在于有界内存快照中；数据库、API 与 Telegram
callback 均不保存或输出这些敏感值。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app import database as db
from app.clients.guangya import GuangYaClient
from app.config import get
from app.logger import get_logger, redact_sensitive_text

logger = get_logger(__name__)

SHARE_PREVIEW_TTL_SECONDS = 15 * 60
SHARE_PREVIEW_MAX_ENTRIES = 256
SHARE_ACTION_MAX_ENTRIES = 4096


@dataclass(frozen=True)
class ShareTransferSnapshot:
    preview_id: str
    owner_key: str
    share_id: str
    access_token: str
    files: tuple[dict[str, Any], ...]
    selected_ids: tuple[str, ...]
    target_id: str
    target_name: str
    expires_at: float


@dataclass
class _PreviewEntry:
    preview_id: str
    owner_key: str
    share_id: str
    access_token: str
    files: dict[str, dict[str, Any]]
    file_order: tuple[str, ...]
    selected_ids: set[str]
    target_id: str
    target_name: str
    expires_at: float
    consumed_selection: tuple[str, ...] | None = None
    consumed_target_id: str = ""


@dataclass(frozen=True)
class _ShareAction:
    action_id: str
    preview_id: str
    owner_key: str
    action: str
    value: Any
    expires_at: float


class ShareTransferPreviewStore:
    """按 chat/user 隔离的 15 分钟有界分享预览与 opaque action store。"""

    def __init__(
        self,
        ttl_seconds: float = SHARE_PREVIEW_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(24),
        action_token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(18),
        max_entries: int = SHARE_PREVIEW_MAX_ENTRIES,
        max_actions: int = SHARE_ACTION_MAX_ENTRIES,
    ) -> None:
        self._ttl_seconds = max(1.0, float(ttl_seconds))
        self._clock = clock
        self._token_factory = token_factory
        self._action_token_factory = action_token_factory
        self._max_entries = max(1, int(max_entries))
        self._max_actions = max(1, int(max_actions))
        self._entries: dict[str, _PreviewEntry] = {}
        self._actions: dict[str, _ShareAction] = {}
        self._lock = threading.RLock()

    @property
    def ttl_seconds(self) -> int:
        return int(self._ttl_seconds)

    @staticmethod
    def owner_key(chat_id: str, user_id: str = "") -> str:
        chat = str(chat_id or "").strip()
        user = str(user_id or "").strip()
        if not chat:
            raise ValueError("缺少预览所有者")
        return f"{chat}\x1f{user}"

    @staticmethod
    def _normalize_file(item: Any) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        file_id = str(item.get("id") or item.get("file_id") or "").strip()
        if not file_id:
            return None
        try:
            size = max(0, int(item.get("size") or 0))
        except (TypeError, ValueError):
            size = 0
        return {
            "id": file_id,
            "name": str(item.get("name") or "未命名项目"),
            "is_dir": bool(item.get("is_dir") or item.get("isDir")),
            "size": size,
        }

    def _drop_preview_locked(self, preview_id: str) -> None:
        self._entries.pop(preview_id, None)
        for action_id in [
            key for key, action in self._actions.items()
            if action.preview_id == preview_id
        ]:
            self._actions.pop(action_id, None)

    def _prune_locked(self, now: float) -> None:
        for preview_id in [
            key for key, entry in self._entries.items()
            if entry.expires_at <= now
        ]:
            self._drop_preview_locked(preview_id)
        for action_id in [
            key for key, action in self._actions.items()
            if action.expires_at <= now or action.preview_id not in self._entries
        ]:
            self._actions.pop(action_id, None)

    def _entry_locked(
        self,
        preview_id: str,
        chat_id: str,
        user_id: str = "",
        *,
        mutable: bool = False,
    ) -> _PreviewEntry:
        now = self._clock()
        self._prune_locked(now)
        key = str(preview_id or "").strip()
        entry = self._entries.get(key)
        if entry is None or entry.owner_key != self.owner_key(chat_id, user_id):
            # 不区分不存在与越权，避免泄露其他会话的 preview 是否存在。
            raise ValueError("预览已过期或无效，请重新解析")
        if mutable and entry.consumed_selection is not None:
            raise ValueError("预览已使用，请重新解析")
        return entry

    def create(self, inspected: dict, chat_id: str, user_id: str = "") -> str:
        share_id = str((inspected or {}).get("share_id") or "").strip()
        access_token = str((inspected or {}).get("access_token") or "").strip()
        if not share_id or not access_token:
            raise ValueError("分享预览缺少必要信息")
        normalized: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for raw in (inspected or {}).get("files") or []:
            item = self._normalize_file(raw)
            if item and item["id"] not in normalized:
                normalized[item["id"]] = item
                order.append(item["id"])
        if not order:
            raise ValueError("分享中没有可转存文件")
        preview_id = str(self._token_factory() or "").strip()
        if not preview_id:
            raise RuntimeError("无法生成分享预览标识")
        now = self._clock()
        entry = _PreviewEntry(
            preview_id=preview_id,
            owner_key=self.owner_key(chat_id, user_id),
            share_id=share_id,
            access_token=access_token,
            files=normalized,
            file_order=tuple(order),
            selected_ids=set(order),
            target_id="0",
            target_name="根目录",
            expires_at=now + self._ttl_seconds,
        )
        with self._lock:
            self._prune_locked(now)
            self._drop_preview_locked(preview_id)
            self._entries[preview_id] = entry
            while len(self._entries) > self._max_entries:
                self._drop_preview_locked(next(iter(self._entries)))
        return preview_id

    def snapshot(self, preview_id: str, chat_id: str, user_id: str = "") -> dict[str, Any]:
        with self._lock:
            entry = self._entry_locked(preview_id, chat_id, user_id)
            files = [dict(entry.files[file_id]) for file_id in entry.file_order]
            selected = [file_id for file_id in entry.file_order if file_id in entry.selected_ids]
            return {
                "preview_id": entry.preview_id,
                "share_id": entry.share_id,
                "files": files,
                "selected_ids": selected,
                "target_id": entry.target_id,
                "target_name": entry.target_name,
                "expires_in": max(0, int(entry.expires_at - self._clock())),
                "consumed": entry.consumed_selection is not None,
            }

    def toggle(self, preview_id: str, file_id: str, chat_id: str, user_id: str = "") -> None:
        with self._lock:
            entry = self._entry_locked(preview_id, chat_id, user_id, mutable=True)
            key = str(file_id or "").strip()
            if key not in entry.files:
                raise ValueError("选择中包含不属于该分享的文件")
            if key in entry.selected_ids:
                entry.selected_ids.remove(key)
            else:
                entry.selected_ids.add(key)

    def select_all(self, preview_id: str, chat_id: str, user_id: str = "") -> None:
        with self._lock:
            entry = self._entry_locked(preview_id, chat_id, user_id, mutable=True)
            entry.selected_ids = set(entry.file_order)

    def select_none(self, preview_id: str, chat_id: str, user_id: str = "") -> None:
        with self._lock:
            entry = self._entry_locked(preview_id, chat_id, user_id, mutable=True)
            entry.selected_ids.clear()

    def set_target(
        self,
        preview_id: str,
        target_id: str,
        target_name: str,
        chat_id: str,
        user_id: str = "",
    ) -> None:
        with self._lock:
            entry = self._entry_locked(preview_id, chat_id, user_id, mutable=True)
            entry.target_id = str(target_id or "0").strip() or "0"
            entry.target_name = str(target_name or "根目录").strip() or "根目录"

    @staticmethod
    def _normalize_selected(selected_ids: list[str] | tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted({str(item).strip() for item in selected_ids if str(item).strip()}))

    def consume(
        self,
        preview_id: str,
        chat_id: str,
        user_id: str = "",
        *,
        selected_ids: list[str] | tuple[str, ...] | None = None,
        target_id: str | None = None,
        target_name: str | None = None,
    ) -> ShareTransferSnapshot:
        """锁定预览上下文；相同参数可重复读取以支持幂等响应。"""
        with self._lock:
            entry = self._entry_locked(preview_id, chat_id, user_id)
            selected = self._normalize_selected(
                selected_ids if selected_ids is not None else tuple(entry.selected_ids)
            )
            if not selected:
                raise ValueError("至少选择一个文件")
            unknown = [file_id for file_id in selected if file_id not in entry.files]
            if unknown:
                raise ValueError("选择中包含不属于该分享的文件")
            chosen_target = str(target_id if target_id is not None else entry.target_id or "0").strip() or "0"
            chosen_name = str(target_name if target_name is not None else entry.target_name or "根目录").strip() or "根目录"
            if entry.consumed_selection is None:
                entry.consumed_selection = selected
                entry.consumed_target_id = chosen_target
                entry.selected_ids = set(selected)
                entry.target_id = chosen_target
                entry.target_name = chosen_name
                self._clear_actions_locked(entry.preview_id)
            elif entry.consumed_selection != selected or entry.consumed_target_id != chosen_target:
                raise ValueError("预览已使用，请重新解析")
            files = tuple(dict(entry.files[file_id]) for file_id in selected)
            return ShareTransferSnapshot(
                preview_id=entry.preview_id,
                owner_key=entry.owner_key,
                share_id=entry.share_id,
                access_token=entry.access_token,
                files=files,
                selected_ids=selected,
                target_id=chosen_target,
                target_name=chosen_name,
                expires_at=entry.expires_at,
            )

    def discard(self, preview_id: str, chat_id: str, user_id: str = "") -> None:
        with self._lock:
            self._entry_locked(preview_id, chat_id, user_id)
            self._drop_preview_locked(str(preview_id or "").strip())

    def _clear_actions_locked(self, preview_id: str) -> None:
        for action_id in [
            key for key, action in self._actions.items()
            if action.preview_id == preview_id
        ]:
            self._actions.pop(action_id, None)

    def begin_actions(self, preview_id: str, chat_id: str, user_id: str = "") -> None:
        with self._lock:
            entry = self._entry_locked(preview_id, chat_id, user_id, mutable=True)
            self._clear_actions_locked(entry.preview_id)

    def create_action(
        self,
        preview_id: str,
        chat_id: str,
        user_id: str,
        action: str,
        value: Any = None,
    ) -> str:
        with self._lock:
            entry = self._entry_locked(preview_id, chat_id, user_id, mutable=True)
            action_id = str(self._action_token_factory() or "").strip()
            if not action_id:
                raise RuntimeError("无法生成 Telegram 操作标识")
            self._actions[action_id] = _ShareAction(
                action_id=action_id,
                preview_id=entry.preview_id,
                owner_key=entry.owner_key,
                action=str(action),
                value=value,
                expires_at=entry.expires_at,
            )
            while len(self._actions) > self._max_actions:
                self._actions.pop(next(iter(self._actions)), None)
            return action_id

    def resolve_action(self, action_id: str, chat_id: str, user_id: str = "") -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            key = str(action_id or "").strip()
            action = self._actions.pop(key, None)
            owner = self.owner_key(chat_id, user_id)
            if action is None or action.owner_key != owner:
                raise ValueError("操作已过期或无效")
            self._entry_locked(action.preview_id, chat_id, user_id, mutable=True)
            return {
                "preview_id": action.preview_id,
                "action": action.action,
                "value": action.value,
            }

    @property
    def entry_count(self) -> int:
        with self._lock:
            self._prune_locked(self._clock())
            return len(self._entries)


_store = ShareTransferPreviewStore()


def get_share_transfer_store() -> ShareTransferPreviewStore:
    return _store


def share_request_key(
    share_id: str,
    selected_ids: list[str] | tuple[str, ...],
    target_id: str,
    chat_id: str,
    user_id: str = "",
) -> str:
    canonical = {
        "chat": ShareTransferPreviewStore.owner_key(chat_id, user_id),
        "files": sorted({str(item).strip() for item in selected_ids if str(item).strip()}),
        "share": str(share_id or "").strip(),
        "target": str(target_id or "0").strip() or "0",
    }
    payload = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def inspect_share_for_transfer(
    share_url: str,
    chat_id: str,
    user_id: str = "",
    *,
    client: GuangYaClient | None = None,
    store: ShareTransferPreviewStore | None = None,
) -> dict[str, Any]:
    client = client or GuangYaClient()
    inspected = client.inspect_share(str(share_url or "").strip())
    store = store or get_share_transfer_store()
    preview_id = store.create(inspected, chat_id, user_id)
    return store.snapshot(preview_id, chat_id, user_id)


def _share_staging_name(request_id: int) -> str:
    token = secrets.token_hex(8)
    return f"MediaFlux-share-{int(request_id)}-{token}"


def _find_share_staging_dir(client: GuangYaClient, parent_id: str, name: str) -> str:
    """按确定性目录名恢复未及时落库的分享隔离目录。"""
    matches: list[str] = []
    for item in client.list_dir(parent_id):
        is_dir = bool(item.get("is_dir")) if isinstance(item, dict) else bool(
            getattr(item, "is_dir", False)
        )
        item_name = str(item.get("name") or "") if isinstance(item, dict) else str(
            getattr(item, "name", "") or ""
        )
        if not is_dir or item_name != name:
            continue
        file_id = str(item.get("file_id") or item.get("id") or "") if isinstance(item, dict) else str(
            getattr(item, "file_id", "") or ""
        )
        if file_id:
            matches.append(file_id)
    unique = list(dict.fromkeys(matches))
    if len(unique) > 1:
        raise RuntimeError("检测到多个同名分享隔离目录，请人工核对后重试")
    return unique[0] if unique else ""


def _result_from_existing(row) -> dict[str, Any]:
    status = str(row["status"] or "")
    return {
        "success": status == "completed" and str(row["gy_status"] or "") == "completed",
        "accepted": status in {"pending", "submitting", "submitted", "downloading"},
        "created": False,
        "duplicate": True,
        "request_id": int(row["id"]),
        "count": 0,
        "target_dir_name": str(row["gy_target_name"] or "根目录"),
        "status": status,
        "error": str(row["error"] or ""),
    }


def create_share_request(
    preview_id: str,
    selected_ids: list[str],
    target_id: str,
    chat_id: str,
    *,
    user_id: str = "",
    target_name: str = "根目录",
    origin: str = "telegram",
    tracker_chat_id: str | None = None,
    client: GuangYaClient | None = None,
    store: ShareTransferPreviewStore | None = None,
) -> dict[str, Any]:
    """二次校验选集并执行一次幂等分享转存。"""
    store = store or get_share_transfer_store()
    snapshot = store.consume(
        preview_id,
        chat_id,
        user_id,
        selected_ids=selected_ids,
        target_id=target_id,
        target_name=target_name,
    )
    # 进入数据库前再次只从已锁定快照构造 canonical IDs，不信任调用方列表。
    canonical_ids = tuple(sorted(file["id"] for file in snapshot.files))
    key = share_request_key(
        snapshot.share_id,
        canonical_ids,
        snapshot.target_id,
        chat_id,
        user_id,
    )
    names = [redact_sensitive_text(file["name"] or "未命名项目") for file in snapshot.files]
    title = redact_sensitive_text(f"分享转存 {len(names)} 项: {', '.join(names[:3])}")
    persisted_target_name = redact_sensitive_text(snapshot.target_name)
    request_id, created = db.create_share_transfer_request(
        key,
        title=title,
        chat_id=str(tracker_chat_id or ""),
        origin=str(origin or "telegram"),
    )
    retried = False
    if not created:
        row = db.get_download_request(request_id)
        if row is None:
            raise RuntimeError("分享转存请求状态丢失")
        if str(row["status"] or "") == "failed" and db.claim_failed_share_transfer_request(
            request_id
        ):
            created = True
            retried = True
        else:
            return _result_from_existing(row)

    client = client or GuangYaClient()
    organize_target = str(get("GY_ORGANIZE_TARGET_DIR", "") or "").strip()
    auto_follow_up = bool(
        organize_target not in {"", "0"}
        and snapshot.target_id not in {"", "0"}
    )
    if not client.logged_in:
        error = "光鸭未登录，请先重新登录"
        db.finish_share_transfer_request(
            request_id,
            success=False,
            target_dir_id=snapshot.target_id,
            target_dir_name=persisted_target_name,
            title=title,
            error=error,
        )
        return {
            "success": False, "created": True, "duplicate": False,
            "retried": retried,
            "request_id": request_id, "count": 0, "status": "failed", "error": error,
        }

    effective_target_id = snapshot.target_id
    effective_target_name = persisted_target_name
    staging_parent_id = ""
    staging_name = ""
    isolated = False
    if auto_follow_up:
        existing = db.get_download_request(request_id)
        existing_staging = str(existing["gy_target_dir"] or "") if existing else ""
        if existing and int(existing["gy_isolated"] or 0) and existing_staging:
            effective_target_id = existing_staging
            staging_parent_id = str(existing["gy_staging_parent_dir"] or snapshot.target_id)
            staging_name = str(existing["gy_staging_name"] or _share_staging_name(request_id))
            effective_target_name = str(
                existing["gy_target_name"]
                or f"{persisted_target_name} / {staging_name}"
            )
            isolated = True
        else:
            staging_parent_id = str(
                (existing["gy_staging_parent_dir"] if existing else "")
                or snapshot.target_id
            )
            staging_name = str(
                (existing["gy_staging_name"] if existing else "")
                or _share_staging_name(request_id)
            )
            if not existing or not str(existing["gy_staging_name"] or ""):
                # 先持久化不可预测的隔离目录身份，再触发云端创建。这样即使
                # Provider 已建目录而进程在 ID 落库前退出，重试也只会认领
                # 这个请求预先分配的目录名。
                db.update_download_request(
                    request_id, gy_staging_parent_dir=staging_parent_id,
                    gy_staging_name=staging_name,
                    gy_staging_cleanup_status="pending",
                    gy_staging_cleanup_error="",
                )
            try:
                effective_target_id = _find_share_staging_dir(
                    client, staging_parent_id, staging_name
                )
                if not effective_target_id:
                    try:
                        effective_target_id = str(
                            client.create_dir(staging_name, staging_parent_id) or ""
                        )
                    except Exception:
                        # Provider 已成功建目录但进程在落库前中断时，重试应复用
                        # 确定性目录，而不是创建重复目录或永久失败。
                        effective_target_id = _find_share_staging_dir(
                            client, staging_parent_id, staging_name
                        )
                        if not effective_target_id:
                            raise
            except Exception as exc:
                error = redact_sensitive_text(f"创建分享隔离目录失败：{exc}")
                db.finish_share_transfer_request(
                    request_id, success=False, target_dir_id=snapshot.target_id,
                    target_dir_name=persisted_target_name, title=title, error=error,
                    failure_status="failed",
                )
                return {
                    "success": False, "created": True, "duplicate": False,
                    "retried": retried, "request_id": request_id, "count": 0,
                    "status": "failed", "error": error,
                }
            if not effective_target_id:
                error = "创建分享隔离目录失败"
                db.finish_share_transfer_request(
                    request_id, success=False, target_dir_id=snapshot.target_id,
                    target_dir_name=persisted_target_name, title=title, error=error,
                    failure_status="failed",
                )
                return {
                    "success": False, "created": True, "duplicate": False,
                    "retried": retried, "request_id": request_id, "count": 0,
                    "status": "failed", "error": error,
                }
            effective_target_name = f"{persisted_target_name} / {staging_name}"
            isolated = True
            db.update_download_request(
                request_id, gy_target_dir=effective_target_id,
                gy_target_name=effective_target_name, gy_isolated=1,
                gy_staging_parent_dir=staging_parent_id,
                gy_staging_name=staging_name, gy_staging_cleanup_status="pending",
                gy_staging_cleanup_error="",
            )

    try:
        result = client.restore_share(
            snapshot.access_token,
            list(canonical_ids),
            effective_target_id,
        )
        success = bool(result.get("success"))
        retry_safe = bool(result.get("retry_safe", False))
        failure_status = "failed" if retry_safe else "manual_review"
        error = "" if success else (
            "光鸭分享转存失败，可重新确认后重试"
            if retry_safe
            else "光鸭转存结果不确定，为避免重复转存已停止重试，请到目标目录核对"
        )
    except Exception as exc:
        logger.warning("光鸭分享转存请求 #%s 失败 (%s)", request_id, type(exc).__name__)
        success = False
        failure_status = "manual_review"
        error = "光鸭转存结果不确定，为避免重复转存已停止重试，请到目标目录核对"

    db.finish_share_transfer_request(
        request_id,
        success=success,
        target_dir_id=effective_target_id,
        target_dir_name=effective_target_name,
        title=title,
        count=len(canonical_ids),
        error=redact_sensitive_text(error),
        failure_status=failure_status if not success else "failed",
        isolated=isolated,
        staging_parent_dir=staging_parent_id,
        staging_name=staging_name,
        staging_cleanup_status="pending" if isolated else "",
    )
    if success:
        if auto_follow_up:
            # 不直接启动整理/STRM/刷新，只唤醒既有 tracker 按现有配置决策。
            from app.modules.download_tracker import get_download_tracker

            get_download_tracker().reload()
    return {
        "success": success,
        "created": True,
        "duplicate": False,
        "retried": retried,
        "request_id": request_id,
        "count": len(canonical_ids) if success else 0,
        "target_dir_name": snapshot.target_name,
        "status": "completed" if success else failure_status,
        "error": redact_sensitive_text(error),
    }
