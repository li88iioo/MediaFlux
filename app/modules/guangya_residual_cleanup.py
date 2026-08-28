"""光鸭整理来源中的空目录与严格垃圾残留目录安全清理。"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
import time
import unicodedata
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from app.clients.guangya import GuangYaClient, GuangYaFile
from app.config import PATHS
from app.modules.guangya_rename import GuangYaRenamePlanError, GuangYaRenamePlanStale
from app.modules.organize import DEFAULT_ORGANIZE_VIDEO_EXTS
from app.modules.web_secret import get_web_secret
from app.private_files import protect_private_file
from app.repositories.organize_operation_jobs import organize_operation_owner_digest

_PLAN_VERSION = 1
_PLAN_TTL_SECONDS = 15 * 60
_CONFIRMED_TTL_SECONDS = 60 * 60
_TERMINAL_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MANUAL_REVIEW_RETENTION_SECONDS = 30 * 24 * 60 * 60
_MAX_TERMINAL_PLANS = 64
_MAX_ACTIVE_PLANS = 32
_MAX_STORAGE_BYTES = 128 * 1024 * 1024
_MAX_PLAN_BYTES = 16 * 1024 * 1024
_MAX_SCANNED_ITEMS = 100_000
_MAX_SCANNED_DIRS = 5_000
_MAX_RESIDUAL_FILES = 32
_MAX_REVIEW_FILES = 8
_MAX_RESIDUAL_DIRS = 64
_MAX_RESIDUAL_BYTES = 128 * 1024 * 1024
_MAX_CANDIDATES = 500
_SAFE_PLAN_ID = re.compile(r"^[0-9a-f]{32}$")
_FORBIDDEN_NAME_RE = re.compile(r"[\\/:*?\"<>|\x00-\x1f]+")
_DOMAIN_RE = re.compile(
    r"(?i)(?:https?://)?(?:www\.)?"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:com|net|org|tv|cc|me|cn|xyz|site|club|info|top|vip|pro|io)"
)
_AD_TOKEN_RE = re.compile(
    r"(?i)(?:广告|推广|扫码|二维码|最新地址|更多资源|防失联|发布页|解压密码|"
    r"app下载|website|homepage|download|readme|url)"
)
_SYSTEM_JUNK = {".ds_store", "thumbs.db", "desktop.ini"}
_IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif", "bmp"}
_LINK_EXTS = {"url", "website", "html", "htm"}
_TEXT_EXTS = {"txt"}
_PROTECTED_MEDIA_IMAGE_STEMS = {
    "poster", "fanart", "cover", "folder", "thumb", "thumbnail", "backdrop",
    "banner", "landscape", "logo", "clearlogo", "disc", "discart",
}


class GuangYaCleanupPlanError(GuangYaRenamePlanError):
    """可安全映射给 Agent 的残留清理错误。"""


class GuangYaCleanupPlanStale(GuangYaRenamePlanStale, GuangYaCleanupPlanError):
    """残留清理计划已失效。"""


@dataclass
class _Node:
    item: GuangYaFile
    source_index: int
    source_name: str
    parent_path: str
    path: str
    children: list[str] = field(default_factory=list)
    files: list[GuangYaFile] = field(default_factory=list)


@dataclass
class _TreeSummary:
    file_count: int = 0
    dir_count: int = 1
    total_size: int = 0
    has_video: bool = False
    has_unsafe: bool = False

    @property
    def qualifies(self) -> bool:
        return bool(
            self.file_count > 0
            and not self.has_video
            and not self.has_unsafe
            and self.file_count <= _MAX_REVIEW_FILES
            and self.dir_count <= _MAX_RESIDUAL_DIRS
            and self.total_size <= _MAX_RESIDUAL_BYTES
        )


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _plan_directory() -> Path:
    return Path(PATHS.data_dir) / "agent-guangya-cleanup"


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise GuangYaCleanupPlanError("残留清理计划目录不可用")
    if os.name == "posix":
        path.chmod(0o700)


def _plan_path(plan_id: str) -> Path:
    if not _SAFE_PLAN_ID.fullmatch(str(plan_id or "")):
        raise GuangYaCleanupPlanError("残留清理计划编号无效")
    return _plan_directory() / f"{plan_id}.json"


def _journal_path(plan_id: str) -> Path:
    if not _SAFE_PLAN_ID.fullmatch(str(plan_id or "")):
        raise GuangYaCleanupPlanError("残留清理计划编号无效")
    return _plan_directory() / f"{plan_id}.jsonl"


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        {key: value for key, value in payload.items() if key != "auth"},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _auth(payload: dict[str, Any]) -> str:
    secret = str(get_web_secret() or "").encode("utf-8")
    if not secret:
        raise GuangYaCleanupPlanError("残留清理计划签名密钥不可用")
    return hmac.new(
        secret, b"mediaflux-guangya-cleanup-plan:v1\0" + _canonical(payload),
        hashlib.sha256,
    ).hexdigest()


def _atomic_write(payload: dict[str, Any]) -> None:
    path = _plan_path(str(payload.get("plan_id") or ""))
    _ensure_private_directory(path.parent)
    stored = dict(payload)
    stored["auth"] = _auth(stored)
    encoded = (json.dumps(stored, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(encoded) > _MAX_PLAN_BYTES:
        raise GuangYaCleanupPlanError("残留清理计划过大，请降低候选上限")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
    replaced = False
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        replaced = True
        if not protect_private_file(path):
            raise GuangYaCleanupPlanError("残留清理计划文件权限不安全")
    except BaseException:
        try:
            os.unlink(path if replaced else temporary)
        except FileNotFoundError:
            pass
        raise


def _read(plan_id: str) -> dict[str, Any]:
    path = _plan_path(plan_id)
    try:
        if path.is_symlink() or not path.is_file():
            raise GuangYaCleanupPlanError("残留清理计划不存在或已过期")
        raw = path.read_text(encoding="utf-8")
        if len(raw.encode("utf-8")) > _MAX_PLAN_BYTES:
            raise GuangYaCleanupPlanError("残留清理计划文件异常")
        payload = json.loads(raw)
    except FileNotFoundError as exc:
        raise GuangYaCleanupPlanError("残留清理计划不存在或已过期") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GuangYaCleanupPlanError("残留清理计划文件损坏") from exc
    if not isinstance(payload, dict) or int(payload.get("version") or 0) != _PLAN_VERSION:
        raise GuangYaCleanupPlanError("残留清理计划版本无效")
    if not hmac.compare_digest(str(payload.get("auth") or ""), _auth(payload)):
        raise GuangYaCleanupPlanError("残留清理计划完整性校验失败")
    return payload


def _append_journal(plan_id: str, event: dict[str, Any]) -> None:
    path = _journal_path(plan_id)
    _ensure_private_directory(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(
                {"at": _now_iso(), **event}, ensure_ascii=False, separators=(",", ":")
            ) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        protect_private_file(path)


def _owner_digest(owner: str) -> str:
    value = str(owner or "").strip()
    if not value:
        raise GuangYaCleanupPlanError("残留清理计划缺少会话身份")
    return organize_operation_owner_digest(value)


def discard_cleanup_plan(plan_id: str) -> None:
    for path in (_plan_path(plan_id), _journal_path(plan_id)):
        try:
            if path.is_file() and not path.is_symlink():
                path.unlink()
        except (FileNotFoundError, OSError):
            continue


def maintain_cleanup_plans() -> dict[str, int]:
    directory = _plan_directory()
    if not directory.exists() or directory.is_symlink():
        return {"removed": 0, "remaining": 0, "active": 0, "bytes": 0}
    current = time.time()
    removed = 0
    total_bytes = 0
    active = 0
    plan_ids: set[str] = set()
    terminal: list[tuple[float, str, int]] = []
    for path in directory.glob("*.json"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            plan_ids.add(path.stem)
            journal = _journal_path(path.stem)
            size = int(path.stat().st_size) + (
                int(journal.stat().st_size)
                if journal.is_file() and not journal.is_symlink() else 0
            )
            try:
                payload = _read(path.stem)
            except GuangYaCleanupPlanError:
                payload = {}
            status = str(payload.get("status") or "").casefold()
            updated = max(
                float(payload.get("created_at_epoch") or 0), float(path.stat().st_mtime)
            )
            remove = False
            if status == "preview":
                remove = float(payload.get("expires_at_epoch") or 0) <= current
            elif status == "confirmed":
                remove = float(payload.get("execute_until_epoch") or 0) <= current
            elif status == "manual_review":
                remove = updated <= current - _MANUAL_REVIEW_RETENTION_SECONDS
                if not remove:
                    terminal.append((updated, path.stem, size))
            elif status in {"completed", "partial", "failed", "cancelled"}:
                remove = updated <= current - _TERMINAL_RETENTION_SECONDS
                if not remove:
                    terminal.append((updated, path.stem, size))
            elif not status and updated <= current - _TERMINAL_RETENTION_SECONDS:
                remove = True
            if remove:
                discard_cleanup_plan(path.stem)
                plan_ids.discard(path.stem)
                removed += 1
            else:
                total_bytes += size
                if status in {"preview", "confirmed", "running"}:
                    active += 1
        except OSError:
            continue
    for journal in directory.glob("*.jsonl"):
        try:
            if (
                journal.is_file() and not journal.is_symlink()
                and journal.stem not in plan_ids
            ):
                journal.unlink()
                removed += 1
        except OSError:
            continue

    terminal.sort()
    while terminal and (
        len(terminal) > _MAX_TERMINAL_PLANS or total_bytes > _MAX_STORAGE_BYTES
    ):
        _updated, plan_id, size = terminal.pop(0)
        discard_cleanup_plan(plan_id)
        total_bytes = max(0, total_bytes - size)
        removed += 1
    remaining = sum(
        1 for path in directory.glob("*.json")
        if path.is_file() and not path.is_symlink()
    )
    return {
        "removed": removed, "remaining": remaining,
        "active": active, "bytes": total_bytes,
    }


def load_cleanup_plan(
    plan_id: str,
    *,
    owner: str | None = None,
    expected_fingerprint: str = "",
    require_confirmed: bool = False,
) -> dict[str, Any]:
    payload = _read(plan_id)
    if owner is not None and not hmac.compare_digest(
        str(payload.get("owner_digest") or ""), _owner_digest(owner)
    ):
        raise GuangYaCleanupPlanError("残留清理计划不属于当前会话")
    if expected_fingerprint and not hmac.compare_digest(
        str(payload.get("fingerprint") or ""), str(expected_fingerprint)
    ):
        raise GuangYaCleanupPlanStale("残留清理计划已变化，请重新预览")
    current = time.time()
    if require_confirmed:
        if (
            float(payload.get("confirmed_at_epoch") or 0) <= 0
            or float(payload.get("execute_until_epoch") or 0) <= current
        ):
            raise GuangYaCleanupPlanStale("残留清理确认已过期，请重新预览")
    elif float(payload.get("expires_at_epoch") or 0) <= current:
        raise GuangYaCleanupPlanStale("残留清理预览已过期，请重新生成")
    return payload


def confirm_cleanup_plan(
    plan_id: str, *, owner: str, expected_fingerprint: str
) -> dict[str, Any]:
    payload = load_cleanup_plan(
        plan_id, owner=owner, expected_fingerprint=expected_fingerprint
    )
    stats = dict(payload.get("stats") or {})
    if max(0, int(stats.get("undecided_count") or 0)) > 0:
        raise GuangYaCleanupPlanError("仍有候选尚未逐项复核，不能确认执行")
    if not list(payload.get("residuals") or []) and not list(payload.get("empties") or []):
        raise GuangYaCleanupPlanError("当前冻结计划没有需要执行的清理对象")
    current = time.time()
    payload.update({
        "status": "confirmed",
        "confirmed_at": _now_iso(),
        "confirmed_at_epoch": current,
        "execute_until_epoch": current + _CONFIRMED_TTL_SECONDS,
    })
    _atomic_write(payload)
    return payload


def _update_execution(plan_id: str, status: str, execution: dict[str, Any]) -> None:
    payload = _read(plan_id)
    payload["status"] = status
    payload["execution"] = dict(execution)
    payload["updated_at"] = _now_iso()
    _atomic_write(payload)


def _extension(item: GuangYaFile) -> str:
    declared = str(item.extension or "").strip().lower().lstrip(".")
    if declared:
        return declared
    match = re.search(r"[.。．]([A-Za-z0-9]{1,10})$", item.name)
    return match.group(1).lower() if match else ""


def _safe_public_name(value: object, fallback: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = _FORBIDDEN_NAME_RE.sub(" ", text)
    text = " ".join(text.split()).strip(" .-_")
    return (text or fallback)[:120].rstrip(" .-_") or fallback


def _configured_video_exts() -> set[str]:
    from app import config

    raw = str(config.get("GY_ORGANIZE_VIDEO_EXTS", "") or "")
    if not raw.strip():
        return set(DEFAULT_ORGANIZE_VIDEO_EXTS)
    values = {
        item.strip().lower().lstrip(".")
        for item in re.split(r"[,，\s]+", raw)
        if re.fullmatch(r"[A-Za-z0-9]{1,10}", item.strip().lstrip("."))
    }
    return values or set(DEFAULT_ORGANIZE_VIDEO_EXTS)


def _supports_guarded_empty_delete(client: Any) -> bool:
    explicit = getattr(client, "supports_guarded_empty_directory_delete", None)
    if explicit is None:
        explicit = getattr(client, "supports_atomic_empty_directory_delete", None)
    if explicit is not None:
        return bool(explicit)
    return callable(getattr(client, "delete_empty_directory", None))


def _is_cleanup_review_candidate(item: GuangYaFile) -> bool:
    """只判断文件是否可以交给 Agent 按文件名复核，不直接判定为垃圾。"""
    name = str(item.name or "")
    lowered = name.casefold()
    if lowered in _SYSTEM_JUNK:
        return True
    ext = _extension(item)
    stem = re.sub(r"[.。．][^.。．]+$", "", name).strip().casefold()
    if ext in _LINK_EXTS:
        return True
    if ext in _TEXT_EXTS:
        return bool(_DOMAIN_RE.search(name) or _AD_TOKEN_RE.search(name))
    if ext in _IMAGE_EXTS:
        normalized_stem = re.sub(r"[^a-z0-9]+", "", stem)
        if any(token in normalized_stem for token in _PROTECTED_MEDIA_IMAGE_STEMS):
            return False
        return True
    return False


def _snapshot(item: GuangYaFile) -> dict[str, Any]:
    return {
        "file_id": str(item.file_id),
        "parent_id": str(item.parent_id),
        "name": str(item.name),
        "is_dir": bool(item.is_dir),
        "size": max(0, int(item.size or 0)),
        "etag": str(item.etag or ""),
        "updated_at": max(0, int(item.updated_at or 0)),
    }


def _signature(entries: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(
        sorted(entries, key=lambda row: (row["parent_id"], row["name"].casefold(), row["file_id"])),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")).hexdigest()


def _collect_tree_entries(node_id: str, nodes: dict[str, _Node]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    stack = [node_id]
    while stack:
        current_id = stack.pop()
        node = nodes[current_id]
        entries.append(_snapshot(node.item))
        entries.extend(_snapshot(item) for item in node.files)
        stack.extend(reversed(node.children))
    return entries


def build_cleanup_plan(
    client: GuangYaClient,
    *,
    owner: str,
    sources: list[dict[str, str]],
    max_candidates: int = 200,
) -> dict[str, Any]:
    normalized_sources: list[dict[str, str]] = []
    for index, source in enumerate(sources, start=1):
        source_id = str(source.get("id") or "").strip()
        if not source_id or source_id == "0":
            raise GuangYaCleanupPlanError("整理来源 ID 无效")
        if all(item["id"] != source_id for item in normalized_sources):
            normalized_sources.append({
                "id": source_id,
                "name": str(source.get("name") or f"来源{index}"),
            })
    sources = normalized_sources
    if not sources:
        raise GuangYaCleanupPlanError("未配置光鸭整理来源")
    safe_max = max(1, min(int(max_candidates), _MAX_CANDIDATES))
    video_exts = _configured_video_exts()
    nodes: dict[str, _Node] = {}
    source_children: dict[int, list[str]] = {}
    scanned_items = 0
    scanned_dirs = 0
    protected_source_ids = {
        str(source.get("id") or "").strip() for source in sources
        if str(source.get("id") or "").strip()
    }
    protected_boundaries = 0

    for source_index, source in enumerate(sources, start=1):
        source_id = str(source.get("id") or "").strip()
        source_name = str(source.get("name") or f"来源{source_index}")
        queue = deque([(source_id, "/")])
        source_children[source_index] = []
        while queue:
            parent_id, parent_path = queue.popleft()
            items = client.list_dir(parent_id)
            for item in items:
                scanned_items += 1
                if scanned_items > _MAX_SCANNED_ITEMS:
                    raise GuangYaCleanupPlanError("整理来源项目数量超过安全上限")
                if not item.is_dir:
                    if parent_id in nodes:
                        nodes[parent_id].files.append(item)
                    continue
                if str(item.file_id) in protected_source_ids:
                    protected_boundaries += 1
                    continue
                scanned_dirs += 1
                if scanned_dirs > _MAX_SCANNED_DIRS:
                    raise GuangYaCleanupPlanError("整理来源目录数量超过安全上限")
                path = parent_path.rstrip("/") + "/" + item.name
                node = _Node(item, source_index, source_name, parent_path, path)
                nodes[str(item.file_id)] = node
                if parent_id in nodes:
                    nodes[parent_id].children.append(str(item.file_id))
                else:
                    source_children[source_index].append(str(item.file_id))
                queue.append((str(item.file_id), path))

    summaries: dict[str, _TreeSummary] = {}
    for node_id in reversed(list(nodes)):
        node = nodes[node_id]
        summary = _TreeSummary()
        for item in node.files:
            summary.file_count += 1
            summary.total_size += max(0, int(item.size or 0))
            if _extension(item) in video_exts:
                summary.has_video = True
            elif not _is_cleanup_review_candidate(item):
                summary.has_unsafe = True
        for child_id in node.children:
            child = summaries[child_id]
            summary.file_count += child.file_count
            summary.dir_count += child.dir_count
            summary.total_size += child.total_size
            summary.has_video = summary.has_video or child.has_video
            summary.has_unsafe = summary.has_unsafe or child.has_unsafe
        summaries[node_id] = summary

    residual_ids: list[str] = []
    empty_ids: list[str] = []
    preserved_dirs = protected_boundaries
    unsupported_empty_dirs = 0
    supports_empty_delete = _supports_guarded_empty_delete(client)

    def select(node_id: str, blocked: bool = False) -> None:
        nonlocal preserved_dirs, unsupported_empty_dirs
        node = nodes[node_id]
        summary = summaries[node_id]
        if not blocked and summary.qualifies:
            residual_ids.append(node_id)
            return
        if not node.files and not node.children:
            if not supports_empty_delete:
                unsupported_empty_dirs += 1
                preserved_dirs += 1
            elif str(node.item.etag or "") or int(node.item.updated_at or 0) > 0:
                empty_ids.append(node_id)
            else:
                preserved_dirs += 1
            return
        if summary.has_video or summary.has_unsafe or node.files:
            preserved_dirs += 1
        for child_id in node.children:
            select(child_id, blocked=False)

    for child_ids in source_children.values():
        for node_id in child_ids:
            select(node_id)

    deferred_candidate_count = max(0, len(residual_ids) - safe_max)
    residual_ids = residual_ids[:safe_max]
    empty_capacity = max(0, _MAX_CANDIDATES - len(residual_ids))
    deferred_empty_dir_count = max(0, len(empty_ids) - empty_capacity)
    empty_ids = empty_ids[:empty_capacity]
    candidates: list[dict[str, Any]] = []
    for index, node_id in enumerate(residual_ids, start=1):
        node = nodes[node_id]
        tree = _collect_tree_entries(node_id, nodes)
        candidates.append({
            "candidate_number": index,
            "root": _snapshot(node.item),
            "tree": tree,
            "signature": _signature(tree),
            "container_name": f"{index:03d}-{_safe_public_name(node.item.name, '残留目录')}",
            "source_index": node.source_index,
            "file_count": summaries[node_id].file_count,
            "total_size": summaries[node_id].total_size,
            "file_names": [
                str(entry.get("name") or "")
                for entry in tree
                if not bool(entry.get("is_dir"))
            ],
        })
    empties = [
        {"root": _snapshot(nodes[node_id].item), "depth": nodes[node_id].path.count("/")}
        for node_id in empty_ids
    ]
    empties.sort(key=lambda row: (-int(row["depth"]), row["root"]["name"].casefold()))

    plan_id = uuid.uuid4().hex
    current = time.time()
    fingerprint_payload = {
        "owner_digest": _owner_digest(owner),
        "credential_generation": int(client.credential_generation),
        "sources": [str(item.get("id") or "") for item in sources],
        "candidates": candidates,
        "candidate_decisions": {},
        "residuals": [],
        "empties": empties,
    }
    fingerprint = hashlib.sha256(json.dumps(
        fingerprint_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")).hexdigest()
    payload: dict[str, Any] = {
        "version": _PLAN_VERSION,
        "plan_id": plan_id,
        "owner_digest": fingerprint_payload["owner_digest"],
        "credential_generation": fingerprint_payload["credential_generation"],
        "created_at": _now_iso(),
        "created_at_epoch": current,
        "expires_at_epoch": current + _PLAN_TTL_SECONDS,
        "status": "preview",
        "fingerprint": fingerprint,
        "source_ids": fingerprint_payload["sources"],
        "batch_name": datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-") + plan_id[:8],
        "stats": {
            "source_count": len(sources),
            "scanned_items": scanned_items,
            "scanned_dirs": scanned_dirs,
            "empty_dir_count": len(empties),
            "candidate_count": len(candidates),
            "reviewed_count": 0,
            "selected_count": 0,
            "kept_count": 0,
            "undecided_count": len(candidates),
            "deferred_candidate_count": deferred_candidate_count,
            "deferred_empty_dir_count": deferred_empty_dir_count,
            "residual_dir_count": 0,
            "quarantine_file_count": 0,
            "preserved_dir_count": preserved_dirs,
            "unsupported_empty_dir_count": unsupported_empty_dirs,
        },
        "samples": [
            str(item["root"]["name"]) for item in candidates[:3]
        ] + [str(item["root"]["name"]) for item in empties[:2]],
        "candidates": candidates,
        "candidate_decisions": {},
        "residuals": [],
        "empties": empties,
    }
    _atomic_write(payload)
    capacity = maintain_cleanup_plans()
    if (
        int(capacity.get("active") or 0) > _MAX_ACTIVE_PLANS
        or int(capacity.get("bytes") or 0) > _MAX_STORAGE_BYTES
    ):
        discard_cleanup_plan(plan_id)
        raise GuangYaCleanupPlanError("私有残留清理计划空间已满，请稍后重试")
    return payload


def revise_cleanup_plan(
    plan_id: str,
    *,
    owner: str,
    expected_fingerprint: str,
    decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    """按候选编号更新私有冻结计划；未明确隔离的候选始终保留。"""
    plan = load_cleanup_plan(
        plan_id, owner=owner, expected_fingerprint=expected_fingerprint
    )
    if str(plan.get("status") or "preview") != "preview":
        raise GuangYaCleanupPlanStale("残留清理计划已进入执行阶段，请重新预览")
    candidates = [
        dict(item) for item in list(plan.get("candidates") or [])
        if isinstance(item, dict)
    ]
    by_number = {
        int(item.get("candidate_number") or 0): item
        for item in candidates
        if int(item.get("candidate_number") or 0) > 0
    }
    if len(by_number) != len(candidates):
        raise GuangYaCleanupPlanError("残留清理候选编号异常，请重新预览")

    current_decisions = {
        str(key): dict(value)
        for key, value in dict(plan.get("candidate_decisions") or {}).items()
        if isinstance(value, dict)
    }
    seen: set[int] = set()
    for raw in decisions:
        if not isinstance(raw, dict):
            raise GuangYaCleanupPlanError("候选复核结果格式无效")
        try:
            number = int(raw.get("candidate_number") or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise GuangYaCleanupPlanError("候选编号必须是整数") from exc
        if number not in by_number:
            raise GuangYaCleanupPlanError(f"候选 #{number} 不存在或已过期")
        if number in seen:
            raise GuangYaCleanupPlanError(f"候选 #{number} 出现重复决定")
        seen.add(number)
        action = str(raw.get("action") or "").strip().casefold()
        if action not in {"quarantine", "keep"}:
            raise GuangYaCleanupPlanError(f"候选 #{number} 的处理决定无效")
        reason = unicodedata.normalize("NFKC", str(raw.get("reason") or ""))
        reason = " ".join(reason.split())[:160]
        current_decisions[str(number)] = {
            "action": action,
            "reason": reason,
        }

    residuals: list[dict[str, Any]] = []
    selected_file_count = 0
    selected_numbers: list[int] = []
    kept_numbers: list[int] = []
    for number, candidate in sorted(by_number.items()):
        decision = current_decisions.get(str(number)) or {}
        action = str(decision.get("action") or "")
        if action == "quarantine":
            selected_numbers.append(number)
            selected_file_count += max(0, int(candidate.get("file_count") or 0))
            residuals.append({
                key: candidate[key]
                for key in (
                    "candidate_number", "root", "tree", "signature",
                    "container_name", "source_index", "file_count", "total_size",
                )
                if key in candidate
            })
        elif action == "keep":
            kept_numbers.append(number)

    reviewed_count = len(selected_numbers) + len(kept_numbers)
    stats = dict(plan.get("stats") or {})
    stats.update({
        "candidate_count": len(candidates),
        "reviewed_count": reviewed_count,
        "selected_count": len(selected_numbers),
        "kept_count": len(kept_numbers),
        "undecided_count": max(0, len(candidates) - reviewed_count),
        "residual_dir_count": len(residuals),
        "quarantine_file_count": selected_file_count,
    })

    new_plan_id = uuid.uuid4().hex
    current = time.time()
    revised = {
        key: value for key, value in plan.items()
        if key not in {
            "auth", "confirmed_at", "confirmed_at_epoch", "execute_until_epoch",
            "execution", "updated_at",
        }
    }
    revised.update({
        "plan_id": new_plan_id,
        "created_at": _now_iso(),
        "created_at_epoch": current,
        "expires_at_epoch": current + _PLAN_TTL_SECONDS,
        "status": "preview",
        "selection_revision": max(0, int(plan.get("selection_revision") or 0)) + 1,
        "candidate_decisions": current_decisions,
        "residuals": residuals,
        "stats": stats,
        "samples": [
            str(item.get("root", {}).get("name") or "") for item in residuals[:3]
        ] + [
            str(item.get("root", {}).get("name") or "")
            for item in list(plan.get("empties") or [])[:2]
        ],
        "batch_name": datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-")
        + new_plan_id[:8],
    })
    fingerprint_payload = {
        "owner_digest": revised.get("owner_digest"),
        "credential_generation": revised.get("credential_generation"),
        "sources": list(revised.get("source_ids") or []),
        "candidates": candidates,
        "candidate_decisions": current_decisions,
        "residuals": residuals,
        "empties": list(revised.get("empties") or []),
    }
    revised["fingerprint"] = hashlib.sha256(json.dumps(
        fingerprint_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")).hexdigest()
    _atomic_write(revised)
    capacity = maintain_cleanup_plans()
    if (
        int(capacity.get("active") or 0) > _MAX_ACTIVE_PLANS + 1
        or int(capacity.get("bytes") or 0) > _MAX_STORAGE_BYTES + _MAX_PLAN_BYTES
    ):
        discard_cleanup_plan(new_plan_id)
        raise GuangYaCleanupPlanError("私有残留清理计划空间已满，请稍后重试")
    return revised


def _matches(item: GuangYaFile | None, snapshot: dict[str, Any]) -> bool:
    return bool(
        item is not None
        and str(item.file_id) == str(snapshot.get("file_id") or "")
        and str(item.name) == str(snapshot.get("name") or "")
        and bool(item.is_dir) == bool(snapshot.get("is_dir"))
        and max(0, int(item.size or 0)) == max(0, int(snapshot.get("size") or 0))
        and str(item.etag or "") == str(snapshot.get("etag") or "")
    )


def _rescan_tree(client: GuangYaClient, root: dict[str, Any]) -> list[dict[str, Any]]:
    root_id = str(root.get("file_id") or "")
    parent_id = str(root.get("parent_id") or "0")
    current = {str(item.file_id): item for item in client.list_dir(parent_id)}.get(root_id)
    if not _matches(current, root):
        raise GuangYaCleanupPlanStale("残留目录状态已变化，请重新预览")
    entries = [_snapshot(current)]
    queue = deque([root_id])
    while queue:
        directory_id = queue.popleft()
        for item in client.list_dir(directory_id):
            entries.append(_snapshot(item))
            if item.is_dir:
                queue.append(str(item.file_id))
            if len(entries) > _MAX_RESIDUAL_FILES + _MAX_RESIDUAL_DIRS + 1:
                raise GuangYaCleanupPlanStale("残留目录内容已变化，请重新预览")
    return entries


def _find_unique_dir(client: GuangYaClient, parent_id: str, name: str) -> str:
    matches = [
        item for item in client.list_dir(parent_id)
        if item.is_dir and item.name == name
    ]
    if len(matches) > 1:
        raise GuangYaCleanupPlanStale("隔离目录名称不唯一，已停止清理")
    if matches:
        return str(matches[0].file_id)
    created = str(client.create_dir(name, parent_id) or "")
    matches = [
        item for item in client.list_dir(parent_id)
        if item.is_dir and item.name == name
    ]
    if len(matches) != 1 or (created and str(matches[0].file_id) != created):
        raise GuangYaCleanupPlanError("隔离目录创建后校验失败")
    return str(matches[0].file_id)


def _validated_selected_residuals(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """复核逐项决定与执行清单完全一致，防止保留项进入写入阶段。"""
    residuals = [
        dict(item) for item in list(plan.get("residuals") or [])
        if isinstance(item, dict)
    ]
    candidates = [
        dict(item) for item in list(plan.get("candidates") or [])
        if isinstance(item, dict)
    ]
    if not candidates:
        return residuals  # 兼容升级前已经确认但尚未执行的旧计划。
    decisions = dict(plan.get("candidate_decisions") or {})
    by_number = {
        int(item.get("candidate_number") or 0): item
        for item in candidates
        if int(item.get("candidate_number") or 0) > 0
    }
    if len(by_number) != len(candidates):
        raise GuangYaCleanupPlanError("残留清理候选清单异常")
    expected_numbers: set[int] = set()
    for number in by_number:
        action = str(dict(decisions.get(str(number)) or {}).get("action") or "")
        if action == "quarantine":
            expected_numbers.add(number)
        elif action != "keep":
            raise GuangYaCleanupPlanError("残留清理仍有候选未完成复核")
    actual_numbers = {
        int(item.get("candidate_number") or 0) for item in residuals
    }
    if expected_numbers != actual_numbers or len(actual_numbers) != len(residuals):
        raise GuangYaCleanupPlanError("残留清理执行范围与逐项决定不一致")
    for item in residuals:
        number = int(item.get("candidate_number") or 0)
        candidate = by_number[number]
        for key in ("root", "tree", "signature", "source_index"):
            if item.get(key) != candidate.get(key):
                raise GuangYaCleanupPlanError(
                    f"残留候选 #{number} 的冻结快照不一致"
                )
    return residuals


def execute_cleanup_plan(
    payload: dict[str, Any],
    *,
    cancel_check: Callable[[], None] | None = None,
    client_factory: Callable[[], GuangYaClient] = GuangYaClient,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or int(payload.get("version") or 0) != 1:
        raise GuangYaCleanupPlanError("残留清理任务参数无效")
    plan_id = str(payload.get("plan_id") or "")
    plan = load_cleanup_plan(
        plan_id,
        expected_fingerprint=str(payload.get("plan_fingerprint") or ""),
        require_confirmed=True,
    )
    if not hmac.compare_digest(
        str(plan.get("owner_digest") or ""), str(payload.get("owner_digest") or "")
    ):
        raise GuangYaCleanupPlanError("残留清理任务会话不匹配")
    expected_generation = int(payload.get("credential_generation") or -1)
    client = client_factory()
    quarantined = 0
    empty_deleted = 0
    failed = 0
    verification_failed = 0
    precondition_failed = 0
    started_at = _now_iso()
    journal_started = False
    try:
        if not client.logged_in or int(client.credential_generation) != expected_generation:
            raise GuangYaCleanupPlanStale("光鸭登录凭据已变化，请重新预览")
        residuals = _validated_selected_residuals(plan)
        empties = list(plan.get("empties") or [])
        # 全批次预检在任何写入前完成。
        for item in residuals:
            if cancel_check is not None:
                cancel_check()
            current_tree = _rescan_tree(client, dict(item.get("root") or {}))
            if not hmac.compare_digest(
                _signature(current_tree), str(item.get("signature") or "")
            ):
                raise GuangYaCleanupPlanStale("残留目录内容已变化，请重新预览")
        for item in empties:
            if cancel_check is not None:
                cancel_check()
            root = dict(item.get("root") or {})
            parent = {str(row.file_id): row for row in client.list_dir(str(root.get("parent_id") or "0"))}
            current = parent.get(str(root.get("file_id") or ""))
            if not _matches(current, root) or client.list_dir(str(root.get("file_id") or "")):
                raise GuangYaCleanupPlanStale("空目录状态已变化，请重新预览")

        _update_execution(plan_id, "running", {
            "started_at": started_at,
            "quarantined": 0,
            "empty_deleted": 0,
            "failed": 0,
        })
        _append_journal(plan_id, {
            "action": "preflight", "status": "completed",
            "residuals": len(residuals), "empties": len(empties),
        })
        journal_started = True

        batch_id = ""
        if residuals:
            isolation_id = _find_unique_dir(client, "0", "MediaFlux隔离")
            residual_root_id = _find_unique_dir(client, isolation_id, "整理残留")
            batch_id = _find_unique_dir(
                client, residual_root_id, str(plan.get("batch_name") or plan_id[:8])
            )
        for index, item in enumerate(residuals, start=1):
            if cancel_check is not None:
                cancel_check()
            root = dict(item.get("root") or {})
            try:
                current_tree = _rescan_tree(client, root)
                unchanged = hmac.compare_digest(
                    _signature(current_tree), str(item.get("signature") or "")
                )
            except Exception:
                unchanged = False
            if not unchanged:
                failed += 1
                precondition_failed += 1
                _append_journal(plan_id, {
                    "action": "quarantine", "index": index,
                    "file_id": str(root.get("file_id") or ""),
                    "status": "failed",
                    "error_type": "PreWriteSnapshotChanged",
                })
                continue
            container_id = _find_unique_dir(
                client, batch_id, str(item.get("container_name") or f"残留-{index:03d}")
            )
            error_type = ""
            try:
                client.move([str(root.get("file_id") or "")], container_id)
            except Exception as exc:
                error_type = type(exc).__name__
            verification_unavailable = False
            try:
                moved = {
                    str(row.file_id): row for row in client.list_dir(container_id)
                }.get(str(root.get("file_id") or ""))
            except Exception:
                moved = None
                verification_unavailable = True
            applied = _matches(moved, root)
            if applied:
                quarantined += 1
            else:
                failed += 1
                if verification_unavailable or not error_type:
                    verification_failed += 1
                if not error_type:
                    error_type = "PostWriteVerificationFailed"
            _append_journal(plan_id, {
                "action": "quarantine", "index": index,
                "file_id": str(root.get("file_id") or ""),
                "status": "completed" if applied else "failed",
                "error_type": error_type,
            })

        for index, item in enumerate(empties, start=1):
            if cancel_check is not None:
                cancel_check()
            root = dict(item.get("root") or {})
            try:
                parent = {
                    str(row.file_id): row
                    for row in client.list_dir(str(root.get("parent_id") or "0"))
                }
                current = parent.get(str(root.get("file_id") or ""))
                unchanged = bool(
                    _matches(current, root)
                    and not client.list_dir(str(root.get("file_id") or ""))
                )
            except Exception:
                unchanged = False
            if not unchanged:
                failed += 1
                precondition_failed += 1
                _append_journal(plan_id, {
                    "action": "delete_empty", "index": index,
                    "file_id": str(root.get("file_id") or ""),
                    "status": "failed",
                    "error_type": "PreWriteSnapshotChanged",
                })
                continue
            error_type = ""
            try:
                client.delete_empty_directory(
                    str(root.get("file_id") or ""),
                    expected_etag=str(root.get("etag") or ""),
                    expected_updated_at=max(0, int(root.get("updated_at") or 0)),
                )
            except Exception as exc:
                error_type = type(exc).__name__
            verification_unavailable = False
            try:
                parent_items = {
                    str(row.file_id): row
                    for row in client.list_dir(str(root.get("parent_id") or "0"))
                }
            except Exception:
                parent_items = {str(root.get("file_id") or ""): None}
                verification_unavailable = True
            applied = str(root.get("file_id") or "") not in parent_items
            if applied:
                empty_deleted += 1
            else:
                failed += 1
                if verification_unavailable or not error_type:
                    verification_failed += 1
                if not error_type:
                    error_type = "PostWriteVerificationFailed"
            _append_journal(plan_id, {
                "action": "delete_empty", "index": index,
                "file_id": str(root.get("file_id") or ""),
                "status": "completed" if applied else "failed",
                "error_type": error_type,
            })

        status = "completed" if failed == 0 else "partial"
        execution = {
            "started_at": started_at,
            "finished_at": _now_iso(),
            "quarantined": quarantined,
            "empty_deleted": empty_deleted,
            "failed": failed,
            "verification_failed": verification_failed,
            "precondition_failed": precondition_failed,
            "journal": _journal_path(plan_id).name,
        }
        _update_execution(plan_id, status, execution)
        _append_journal(plan_id, {"action": "final", "status": status, **execution})
        return {
            "partial": failed > 0,
            "stats": {
                "total": len(residuals) + len(empties),
                "quarantined": quarantined,
                "empty_deleted": empty_deleted,
                "failed": failed,
                "verification_failed": verification_failed,
                "precondition_failed": precondition_failed,
            },
        }
    except BaseException as exc:
        if journal_started:
            _append_journal(plan_id, {
                "action": "fatal", "status": "failed", "error_type": type(exc).__name__
            })
        try:
            _update_execution(plan_id, "failed", {
                "started_at": started_at,
                "finished_at": _now_iso(),
                "quarantined": quarantined,
                "empty_deleted": empty_deleted,
                "failed": failed,
                "precondition_failed": precondition_failed,
                "error_type": type(exc).__name__,
            })
        except Exception:
            pass
        raise
    finally:
        client.close()
        try:
            maintain_cleanup_plans()
        except Exception:
            pass
