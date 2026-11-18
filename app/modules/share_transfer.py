"""光鸭分享预览、选择状态与幂等转存服务。

分享访问令牌和签名 URL 只存在于有界内存快照中；数据库、API 与 Telegram
callback 均不保存或输出这些敏感值。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import Generator, Any, Callable

from app.repositories import download_requests as download_requests_repo
from app.clients.guangya import GuangYaClient, close_guangya_client
from app.config import get
from app.logger import get_logger, redact_sensitive_text

logger = get_logger(__name__)


@contextmanager
def _guangya_client_scope(
    client: GuangYaClient | None,
) -> Generator[GuangYaClient, None, None]:
    """复用注入 Client，并只释放当前调用内部创建的连接池。"""
    owned_client = client is None
    runtime_client = client or GuangYaClient()
    try:
        yield runtime_client
    finally:
        if owned_client:
            close_guangya_client(runtime_client)


SHARE_PREVIEW_TTL_SECONDS = 15 * 60
SHARE_PREVIEW_MAX_ENTRIES = 256
SHARE_ACTION_MAX_ENTRIES = 4096


class ShareTransferAccountChanged(ValueError):
    def __init__(self) -> None:
        super().__init__("光鸭登录账号已变化，请重新解析分享链接并选择目标目录")


class ShareTransferAccountUnavailable(ValueError):
    def __init__(self) -> None:
        super().__init__("无法核对光鸭账号，请检查登录并重新解析分享链接")


def _share_generation(client: GuangYaClient) -> int | None:
    generation = client.credential_generation
    return generation if client.logged_in else None


def _require_share_generation(client: GuangYaClient, expected: int | None) -> None:
    if expected is None or _share_generation(client) != expected:
        raise ShareTransferAccountChanged()


def _require_share_account(generation: int | None, account_key: str) -> None:
    if generation is None:
        raise ValueError("光鸭未登录，请先登录再重新解析分享链接")
    if not account_key:
        raise ShareTransferAccountUnavailable()


@dataclass(frozen=True)
class ShareTransferSnapshot:
    preview_id: str
    owner_key: str
    share_id: str
    access_token: str
    credential_generation: int | None
    account_key: str
    files: tuple[dict[str, Any], ...]
    selected_ids: tuple[str, ...]
    target_id: str
    target_name: str
    expires_at: float


@dataclass(frozen=True)
class ShareTransferSubmission:
    request_id: int
    snapshot: ShareTransferSnapshot
    title: str
    retried: bool


@dataclass
class _PreviewEntry:
    preview_id: str
    owner_key: str
    share_id: str
    access_token: str
    credential_generation: int | None
    account_key: str
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
        self._max_entries = max(1, max_entries)
        self._max_actions = max(1, max_actions)
        self._entries: dict[str, _PreviewEntry] = {}
        self._actions: dict[str, _ShareAction] = {}
        self._lock = threading.RLock()

    @property
    def ttl_seconds(self) -> int:
        return int(self._ttl_seconds)

    @staticmethod
    def owner_key(chat_id: str | int, user_id: str | int = "") -> str:
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
        chat_id: str | int,
        user_id: str | int = "",
        *,
        mutable: bool = False,
    ) -> _PreviewEntry:
        now = self._clock()
        self._prune_locked(now)
        key = (preview_id or "").strip()
        entry = self._entries.get(key)
        if entry is None or entry.owner_key != self.owner_key(chat_id, user_id):
            # 不区分不存在与越权，避免泄露其他会话的 preview 是否存在。
            raise ValueError("预览已过期或无效，请重新解析")
        if mutable and entry.consumed_selection is not None:
            raise ValueError("预览已使用，请重新解析")
        return entry

    def create(
        self,
        inspected: dict,
        chat_id: str | int,
        user_id: str | int = "",
        *,
        credential_generation: int | None = None,
        account_key: str = "",
        target_id: str = "0",
        target_name: str = "根目录",
    ) -> str:
        share_id = str((inspected or {}).get("share_id") or "").strip()
        access_token = str((inspected or {}).get("access_token") or "").strip()
        if not share_id or not access_token:
            raise ValueError("分享预览缺少必要信息")
        if inspected.get("has_more"):
            raise ValueError("分享文件读取未完成，不能建立转存快照")
        normalized: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for raw in (inspected or {}).get("files") or []:
            item = self._normalize_file(raw)
            if item and item["id"] not in normalized:
                normalized[item["id"]] = item
                order.append(item["id"])
        if not order:
            raise ValueError("分享中没有可转存文件")
        preview_id = (self._token_factory() or "").strip()
        if not preview_id:
            raise RuntimeError("无法生成分享预览标识")
        normalized_target_id = (target_id or "0").strip() or "0"
        normalized_target_name = (target_name or "").strip() or (
            "根目录" if normalized_target_id == "0" else "默认转存目录"
        )
        now = self._clock()
        entry = _PreviewEntry(
            preview_id=preview_id,
            owner_key=self.owner_key(chat_id, user_id),
            share_id=share_id,
            access_token=access_token,
            credential_generation=credential_generation,
            account_key=account_key,
            files=normalized,
            file_order=tuple(order),
            selected_ids=set(order),
            target_id=normalized_target_id,
            target_name=normalized_target_name,
            expires_at=now + self._ttl_seconds,
        )
        with self._lock:
            self._prune_locked(now)
            self._drop_preview_locked(preview_id)
            self._entries[preview_id] = entry
            while len(self._entries) > self._max_entries:
                self._drop_preview_locked(next(iter(self._entries)))
        return preview_id

    def snapshot(self, preview_id: str, chat_id: str | int, user_id: str | int = "") -> dict[str, Any]:
        with self._lock:
            entry = self._entry_locked(preview_id, chat_id, user_id)
            files = [dict(entry.files[file_id]) for file_id in entry.file_order]
            selected = [file_id for file_id in entry.file_order if file_id in entry.selected_ids]
            return {
                "preview_id": entry.preview_id,
                "share_id": entry.share_id,
                "files": files,
                "count": len(files),
                "has_more": False,
                "scope": "top_level",
                "selected_ids": selected,
                "target_id": entry.target_id,
                "target_name": entry.target_name,
                "expires_in": max(0, int(entry.expires_at - self._clock())),
                "consumed": entry.consumed_selection is not None,
            }

    def toggle(self, preview_id: str, file_id: str, chat_id: str | int, user_id: str | int = "") -> None:
        with self._lock:
            entry = self._entry_locked(preview_id, chat_id, user_id, mutable=True)
            key = (file_id or "").strip()
            if key not in entry.files:
                raise ValueError("选择中包含不属于该分享的文件")
            if key in entry.selected_ids:
                entry.selected_ids.remove(key)
            else:
                entry.selected_ids.add(key)

    def select_all(self, preview_id: str, chat_id: str | int, user_id: str | int = "") -> None:
        with self._lock:
            entry = self._entry_locked(preview_id, chat_id, user_id, mutable=True)
            entry.selected_ids = set(entry.file_order)

    def select_none(self, preview_id: str, chat_id: str | int, user_id: str | int = "") -> None:
        with self._lock:
            entry = self._entry_locked(preview_id, chat_id, user_id, mutable=True)
            entry.selected_ids.clear()

    def set_target(
        self,
        preview_id: str,
        target_id: str,
        target_name: str,
        chat_id: str | int,
        user_id: str | int = "",
    ) -> None:
        with self._lock:
            entry = self._entry_locked(preview_id, chat_id, user_id, mutable=True)
            entry.target_id = (target_id or "0").strip() or "0"
            entry.target_name = (target_name or "根目录").strip() or "根目录"

    @staticmethod
    def _normalize_selected(selected_ids: list[str] | tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted({item.strip() for item in selected_ids if item.strip()}))

    def consume(
        self,
        preview_id: str,
        chat_id: str | int,
        user_id: str | int = "",
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
            chosen_target = (target_id if target_id is not None else entry.target_id or "0").strip() or "0"
            chosen_name = (target_name if target_name is not None else entry.target_name or "根目录").strip() or "根目录"
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
                credential_generation=entry.credential_generation,
                account_key=entry.account_key,
                files=files,
                selected_ids=selected,
                target_id=chosen_target,
                target_name=chosen_name,
                expires_at=entry.expires_at,
            )

    def discard(self, preview_id: str, chat_id: str | int, user_id: str | int = "") -> None:
        with self._lock:
            self._entry_locked(preview_id, chat_id, user_id)
            self._drop_preview_locked((preview_id or "").strip())

    def _clear_actions_locked(self, preview_id: str) -> None:
        for action_id in [
            key for key, action in self._actions.items()
            if action.preview_id == preview_id
        ]:
            self._actions.pop(action_id, None)

    def begin_actions(self, preview_id: str, chat_id: str | int, user_id: str | int = "") -> None:
        with self._lock:
            entry = self._entry_locked(preview_id, chat_id, user_id, mutable=True)
            self._clear_actions_locked(entry.preview_id)

    def create_action(
        self,
        preview_id: str,
        chat_id: str | int,
        user_id: str | int,
        action: str,
        value: Any = None,
    ) -> str:
        with self._lock:
            entry = self._entry_locked(preview_id, chat_id, user_id, mutable=True)
            action_id = (self._action_token_factory() or "").strip()
            if not action_id:
                raise RuntimeError("无法生成 Telegram 操作标识")
            self._actions[action_id] = _ShareAction(
                action_id=action_id,
                preview_id=entry.preview_id,
                owner_key=entry.owner_key,
                action=action,
                value=value,
                expires_at=entry.expires_at,
            )
            while len(self._actions) > self._max_actions:
                self._actions.pop(next(iter(self._actions)), None)
            return action_id

    def resolve_action(self, action_id: str, chat_id: str | int, user_id: str | int = "") -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            key = (action_id or "").strip()
            action = self._actions.get(key)
            owner = self.owner_key(chat_id, user_id)
            if action is None or action.owner_key != owner:
                raise ValueError("操作已过期或无效")
            self._entry_locked(action.preview_id, chat_id, user_id, mutable=True)
            del self._actions[key]
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
    *,
    account_key: str = "",
    legacy_owner: str = "",
) -> str:
    canonical = {
        **({"account": account_key} if account_key else {"chat": legacy_owner}),
        "files": sorted({item.strip() for item in selected_ids if item.strip()}),
        "share": (share_id or "").strip(),
        "target": (target_id or "0").strip() or "0",
    }
    payload = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _inspect_share_for_transfer(
    share_url: str,
    chat_id: str | int,
    user_id: str | int,
    *,
    client: GuangYaClient,
    store: ShareTransferPreviewStore,
) -> tuple[dict[str, Any], int | None, str]:
    from app.modules.download_dispatcher import is_guangya_share_url

    if not isinstance(share_url, str) or len(share_url) > 8192 or not is_guangya_share_url(share_url):
        raise ValueError("请提供有效的光鸭分享链接")
    ShareTransferPreviewStore.owner_key(chat_id, user_id)
    generation = _share_generation(client)
    inspected = client.inspect_share(share_url.strip())
    account_key = ""
    if generation is not None:
        _require_share_generation(client, generation)
        try:
            account_key = client.share_account_key()
        except Exception as exc:
            # 分享内容仍可只读查看，转存须取得经认证的账号身份。
            logger.warning("光鸭分享账号核对失败 type=%s", type(exc).__name__)
        _require_share_generation(client, generation)
    target_id = (get("GY_SHARE_TARGET_DIR", "0") or "0").strip() or "0"
    target_name = (
        get("GY_SHARE_TARGET_DIR_NAME", "根目录") or "根目录"
    ).strip() or ("根目录" if target_id == "0" else "默认转存目录")
    preview_id = store.create(
        inspected, chat_id, user_id, credential_generation=generation, account_key=account_key,
        target_id=target_id, target_name=target_name,
    )
    return store.snapshot(preview_id, chat_id, user_id), generation, account_key


def inspect_share_for_transfer(
    share_url: str,
    chat_id: str | int,
    user_id: str | int = "",
    *,
    client: GuangYaClient | None = None,
    store: ShareTransferPreviewStore | None = None,
) -> dict[str, Any]:
    with _guangya_client_scope(client) as runtime_client:
        preview, _, _ = _inspect_share_for_transfer(
            share_url, chat_id, user_id, client=runtime_client, store=store or get_share_transfer_store(),
        )
        return preview


def prepare_share_transfer(
    share_url: str, file_ids: list[str], chat_id: str | int, user_id: str | int = "",
    *, client: GuangYaClient | None = None, store: ShareTransferPreviewStore | None = None,
) -> dict[str, Any]:
    """只读冻结选定的真实文件和当前配置目标；不持久化分享访问令牌。"""
    owner = ShareTransferPreviewStore.owner_key(chat_id, user_id)
    if (not isinstance(file_ids, list) or not file_ids
            or any(not isinstance(item, str) or not item.strip() for item in file_ids)):
        raise ValueError("file_ids 必须是非空文件ID列表")
    selected = sorted({item.strip() for item in file_ids})
    with _guangya_client_scope(client) as runtime_client:
        if not runtime_client.logged_in:
            raise ValueError("光鸭未登录，请先重新登录")
        preview, generation, account_key = _inspect_share_for_transfer(
            share_url, chat_id, user_id, client=runtime_client, store=store or get_share_transfer_store(),
        )
        _require_share_account(generation, account_key)
    files = {item["id"]: item for item in preview["files"]}
    if any(identity not in files for identity in selected):
        raise ValueError("选择中包含不属于该分享的文件")
    return {
        "preview_id": preview["preview_id"],
        "snapshot": {
            "owner": owner, "url": share_url.strip(), "share_id": preview["share_id"],
            "credential_generation": generation,
            "account_key": account_key,
            "files": [files[identity] for identity in selected],
            "target_id": preview["target_id"], "target_name": preview["target_name"],
        },
    }


def submit_prepared_share_transfer(
    prepared: dict[str, Any], chat_id: str | int, *, user_id: str | int = "",
    origin: str = "telegram", tracker_chat_id: str | None = None,
    client: GuangYaClient | None = None, store: ShareTransferPreviewStore | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """冷确认重新获取令牌和核对冻结事实，再使用唯一持久转存核心。"""
    from app.repositories.download_requests import download_request_public_summary

    if (not isinstance(prepared, dict)
            or prepared.get("owner") != ShareTransferPreviewStore.owner_key(chat_id, user_id)):
        raise ValueError("分享确认归属已失效，请重新检查")
    with _guangya_client_scope(client) as runtime_client:
        _require_share_generation(runtime_client, prepared.get("credential_generation"))
        current = prepare_share_transfer(
            prepared["url"], [item["id"] for item in prepared["files"]], chat_id, user_id,
            client=runtime_client, store=store,
        )
        if current["snapshot"] != prepared:
            raise ValueError("光鸭分享内容或目标目录已变化，请重新检查")
        result = create_share_request(
            current["preview_id"], [item["id"] for item in prepared["files"]],
            prepared["target_id"], chat_id, user_id=user_id,
            target_name=prepared["target_name"], origin=origin, tracker_chat_id=tracker_chat_id,
            client=runtime_client, store=store, cancel_check=cancel_check,
        )
    row = download_requests_repo.get_download_request(result["request_id"]) if result["request_id"] else None
    if row is not None:
        result["request"] = download_request_public_summary(row)
    return result


def _share_staging_name(request_id: int) -> str:
    token = secrets.token_hex(8)
    return f"MediaFlux-share-{request_id}-{token}"


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


def _result_from_existing(row, *, public_target: str | None = None, legacy: bool = False) -> dict[str, Any]:
    status = "manual_review" if legacy else str(row["status"] or "")
    private = public_target is None
    error = str(row["error"] or "") if private else ""
    if legacy:
        error = "发现同一分享的历史转存，原光鸭账号无法核对，请先查看下载记录和目标目录"
    elif not private and status in {"failed", "manual_review", "stopped"}:
        error = "相同分享的转存需要核对，请先检查云盘目录"
    return {
        "success": status == "completed" and str(row["gy_status"] or "") == "completed",
        "accepted": status in {"pending", "submitting", "submitted", "downloading"},
        "created": False,
        "duplicate": True,
        "request_id": int(row["id"]) if private else None,
        "count": 0,
        "target_dir_name": str(row["gy_target_name"] or "根目录") if private else public_target,
        "status": status,
        "error": error,
    }


def get_share_request_result(request_id: int) -> dict[str, Any]:
    row = download_requests_repo.get_download_request(request_id)
    if row is None or row["kind"] != "guangya_share":
        raise RuntimeError("分享转存请求状态丢失")
    return _result_from_existing(row)


def accept_share_request(
    preview_id: str,
    chat_id: str | int,
    *,
    user_id: str | int = "",
    selected_ids: list[str] | None = None,
    target_id: str | None = None,
    target_name: str | None = None,
    origin: str = "telegram",
    tracker_chat_id: str | None = None,
    client: GuangYaClient | None = None,
    store: ShareTransferPreviewStore | None = None,
) -> ShareTransferSubmission | dict[str, Any]:
    """冻结选集及目录，持久认领一次转存，或返回同一请求的现有状态。"""
    store = store or get_share_transfer_store()
    snapshot = store.consume(
        preview_id,
        chat_id,
        user_id,
        selected_ids=selected_ids,
        target_id=target_id,
        target_name=target_name,
    )
    if client is not None and (snapshot.credential_generation is not None or client.logged_in):
        _require_share_generation(client, snapshot.credential_generation)
    _require_share_account(snapshot.credential_generation, snapshot.account_key)
    identity = (snapshot.share_id, snapshot.selected_ids, snapshot.target_id)
    legacy_key = share_request_key(*identity, legacy_owner=snapshot.owner_key)
    legacy = download_requests_repo.get_download_request_by_request_key(legacy_key)
    if legacy is not None and legacy["kind"] == "guangya_share":
        # 历史键只能证明同一会话曾提交。不能把旧记录认领给当前账号。
        return _result_from_existing(legacy, public_target=snapshot.target_name, legacy=True)
    key = share_request_key(
        *identity, account_key=snapshot.account_key,
    )
    receipt_owner = hashlib.sha256(snapshot.owner_key.encode()).hexdigest()
    names = [redact_sensitive_text(file["name"] or "未命名项目") for file in snapshot.files]
    title = redact_sensitive_text(f"分享转存 {len(names)} 项: {', '.join(names[:3])}")
    request_id, created = download_requests_repo.create_share_transfer_request(
        key,
        title=title,
        chat_id=(tracker_chat_id or ""),
        origin=(origin or "telegram"),
        receipt_owner=receipt_owner,
    )
    if created:
        return ShareTransferSubmission(request_id, snapshot, title, retried=False)
    row = download_requests_repo.get_download_request(request_id)
    if row is None:
        raise RuntimeError("分享转存请求状态丢失")
    if row["user_id"] != receipt_owner:
        return _result_from_existing(row, public_target=snapshot.target_name)
    if download_requests_repo.claim_failed_share_transfer_request(request_id, receipt_owner=receipt_owner):
        return ShareTransferSubmission(request_id, snapshot, title, retried=True)
    return get_share_request_result(request_id)


def create_share_request(
    preview_id: str,
    selected_ids: list[str],
    target_id: str,
    chat_id: str | int,
    *,
    user_id: str | int = "",
    target_name: str = "根目录",
    origin: str = "telegram",
    tracker_chat_id: str | None = None,
    client: GuangYaClient | None = None,
    store: ShareTransferPreviewStore | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """同步调用者使用与 Telegram 相同的认领和转存链。"""
    if cancel_check is not None:
        cancel_check()
    accepted = accept_share_request(
        preview_id, chat_id, user_id=user_id, selected_ids=selected_ids,
        target_id=target_id, target_name=target_name, origin=origin,
        tracker_chat_id=tracker_chat_id, client=client, store=store,
    )
    if isinstance(accepted, dict):
        return accepted
    return execute_share_request(accepted, client=client, cancel_check=cancel_check)


def execute_share_request(
    submission: ShareTransferSubmission,
    *,
    client: GuangYaClient | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    request_id, snapshot = submission.request_id, submission.snapshot
    title, retried = submission.title, submission.retried
    persisted_target_name = redact_sensitive_text(snapshot.target_name)

    def finish(**fields) -> dict[str, Any] | None:
        if download_requests_repo.finish_share_transfer_request(request_id, **fields):
            return None
        return get_share_request_result(request_id)

    def preparation_failed(error: str) -> dict[str, Any]:
        stale = finish(
            success=False, target_dir_id=effective_target_id if isolated else snapshot.target_id,
            target_dir_name=effective_target_name, title=title, error=error,
            isolated=isolated, staging_parent_dir=staging_parent_id, staging_name=staging_name,
            staging_cleanup_status="retained" if isolated or creating_staging else "",
        )
        if stale is not None:
            return stale
        return {
            "success": False, "created": True, "duplicate": False,
            "retried": retried, "request_id": request_id, "count": 0,
            "status": "failed", "error": error,
        }

    effective_target_id = snapshot.target_id
    effective_target_name = persisted_target_name
    staging_parent_id = ""
    staging_name = ""
    isolated = False
    creating_staging = False
    restore_started = False

    def check_write() -> None:
        if cancel_check is not None:
            cancel_check()
        _require_share_generation(runtime_client, snapshot.credential_generation)

    def before_create() -> None:
        nonlocal creating_staging
        check_write()
        creating_staging = True

    def before_restore() -> None:
        nonlocal restore_started
        check_write()
        restore_started = True

    try:
        with ExitStack() as stack:
            try:
                runtime_client = stack.enter_context(_guangya_client_scope(client))
            except Exception as exc:
                # 尚未尝试任何云写，明确失败允许既有的一次显式重试。
                logger.warning("光鸭分享客户端初始化失败 (%s)", type(exc).__name__)
                return preparation_failed("光鸭客户端初始化失败，请重新确认后重试")
            organize_target = (get("GY_ORGANIZE_TARGET_DIR", "") or "").strip()
            auto_follow_up = (
                organize_target not in {"", "0"}
                and snapshot.target_id not in {"", "0"}
            )
            check_write()
            if auto_follow_up:
                existing = download_requests_repo.get_download_request(request_id)
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
                        download_requests_repo.update_download_request(
                            request_id, gy_staging_parent_dir=staging_parent_id,
                            gy_staging_name=staging_name,
                            gy_staging_cleanup_status="pending",
                            gy_staging_cleanup_error="",
                        )
                    try:
                        effective_target_id = _find_share_staging_dir(
                            runtime_client, staging_parent_id, staging_name
                        )
                        if not effective_target_id:
                            try:
                                effective_target_id = (
                                    runtime_client.create_dir(
                                        staging_name, staging_parent_id, before_write=before_create,
                                    ) or ""
                                )
                                creating_staging = False
                            except ShareTransferAccountChanged:
                                raise
                            except Exception:
                                # Provider 已成功建目录但进程在落库前中断时，重试应复用
                                # 确定性目录，而不是创建重复目录或永久失败。
                                effective_target_id = _find_share_staging_dir(
                                    runtime_client, staging_parent_id, staging_name
                                )
                                if not effective_target_id:
                                    raise
                                creating_staging = False
                    except ShareTransferAccountChanged:
                        raise
                    except Exception as exc:
                        _require_share_generation(runtime_client, snapshot.credential_generation)
                        error = redact_sensitive_text(f"创建分享隔离目录失败：{exc}")
                        return preparation_failed(error)
                    if not effective_target_id:
                        error = "创建分享隔离目录失败"
                        return preparation_failed(error)
                    effective_target_name = f"{persisted_target_name} / {staging_name}"
                    isolated = True
                if not download_requests_repo.bind_download_request_guangya_staging(
                    request_id, staging_id=effective_target_id,
                    parent_id=staging_parent_id, staging_name=staging_name,
                    target_name=effective_target_name,
                ):
                    return get_share_request_result(request_id)

            check_write()
            try:
                # 客户端取得 raw（含凭据刷新）后再检查，只有进入发送边界才算在途。
                result = runtime_client.restore_share(
                    snapshot.access_token, list(snapshot.selected_ids), effective_target_id,
                    cancel_check=before_restore,
                )
                success = bool(result.get("success"))
                retry_safe = bool(result.get("retry_safe", False))
                failure_status = "failed" if retry_safe else "manual_review"
                error = "" if success else (
                    "光鸭分享转存失败，可重新确认后重试"
                    if retry_safe
                    else "光鸭转存结果不确定，为避免重复转存已停止重试，请到目标目录核对"
                )
            except ShareTransferAccountChanged:
                raise
            except Exception as exc:
                if not restore_started:
                    _require_share_generation(runtime_client, snapshot.credential_generation)
                logger.warning("光鸭分享转存请求 #%s 失败 (%s)", request_id, type(exc).__name__)
                success = False
                failure_status = "manual_review"
                error = "光鸭转存结果不确定，为避免重复转存已停止重试，请到目标目录核对"

            stale = finish(
                success=success,
                target_dir_id=effective_target_id,
                target_dir_name=effective_target_name,
                title=title,
                count=len(snapshot.selected_ids),
                error=redact_sensitive_text(error),
                failure_status=failure_status if not success else "failed",
                isolated=isolated,
                staging_parent_dir=staging_parent_id,
                staging_name=staging_name,
                staging_cleanup_status="pending" if isolated else "",
            )
            if stale is not None:
                return stale
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
                "count": len(snapshot.selected_ids) if success else 0,
                "target_dir_name": snapshot.target_name,
                "status": "completed" if success else failure_status,
                "error": redact_sensitive_text(error),
            }
    except ShareTransferAccountChanged as exc:
        rejected = preparation_failed(str(exc))
        if not rejected["created"]:
            return rejected
        raise
    except asyncio.CancelledError:
        # 取消也必须结算认领；已知目录保留身份，发送后的未知结果禁止重放。
        unknown = creating_staging or restore_started
        error = (
            "操作已取消，云端写入结果不确定，请先核对，勿重复转存"
            if unknown else "操作已取消，未提交分享转存"
        )
        retained = isolated or creating_staging
        finish(
            success=False, target_dir_id=effective_target_id if isolated else snapshot.target_id,
            target_dir_name=effective_target_name, title=title, error=error,
            failure_status="manual_review" if unknown else "failed",
            isolated=isolated, staging_parent_dir=staging_parent_id,
            staging_name=staging_name,
            staging_cleanup_status="retained" if retained else "",
            staging_cleanup_error="操作已取消，隔离目录已保留待核对" if retained else "",
        )
        raise
