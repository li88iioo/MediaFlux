"""光鸭受控重命名计划、私有清单与确定性执行。

LLM 只负责选择公开变换；本模块负责解析精确路径、冻结远端快照、排除
重名冲突、持久化可回滚清单，并在确认后的整理互斥队列中逐项写入和复核。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
import time
import uuid
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from app.clients.guangya import GuangYaClient, GuangYaFile, GuangYaWriteRejected
from app.config import PATHS
from app.modules.web_secret import get_web_secret
from app.private_files import protect_private_file
from app.repositories.organize_operation_jobs import organize_operation_owner_digest

_PLAN_VERSION = 1
_PLAN_TTL_SECONDS = 15 * 60
_CONFIRMED_TTL_SECONDS = 60 * 60
_MAX_TARGETS = 4
_MAX_RENAMES = 10_000
_MAX_SCANNED_ITEMS = 100_000
_MAX_SCANNED_DIRS = 5_000
_MAX_NAME_LENGTH = 255
_MAX_PLAN_FILE_BYTES = 16 * 1024 * 1024
_TERMINAL_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MANUAL_REVIEW_RETENTION_SECONDS = 30 * 24 * 60 * 60
_MAX_TERMINAL_PLANS = 128
_MAX_ACTIVE_PLANS = 64
_MAX_PLAN_STORAGE_BYTES = 512 * 1024 * 1024
_SAFE_PLAN_ID = re.compile(r"^[0-9a-f]{32}$")
_COMPACT_BITRATE_RE = re.compile(
    r"\.(?!(?:26[0-9])\.)(?:\d+(?:\.\d+)?)\s*Mbps(?=\.|$)", re.IGNORECASE,
)
_DASH_BITRATE_RE = re.compile(
    r"\s+-\s+(?:\d+(?:\.\d+)?)\s*Mbps(?=\.|[-_\s]|$)", re.IGNORECASE,
)
_SPACED_BITRATE_RE = re.compile(
    r"\s+(?:\d+(?:\.\d+)?)\s*Mbps(?=\.|[-_\s]|$)", re.IGNORECASE,
)


class GuangYaRenamePlanError(RuntimeError):
    """可安全投影给 Agent 的重命名预检错误。"""


class GuangYaRenamePlanStale(GuangYaRenamePlanError):
    """计划快照、凭据或确认上下文已失效。"""


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _plan_directory() -> Path:
    return Path(PATHS.data_dir) / "agent-guangya-rename"


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        if path.is_symlink() or not path.is_dir():
            raise GuangYaRenamePlanError("重命名计划目录不可用")
        if os.name == "posix":
            path.chmod(0o700)
    except OSError as exc:
        raise GuangYaRenamePlanError("重命名计划目录不可用") from exc


def _plan_path(plan_id: str) -> Path:
    if not _SAFE_PLAN_ID.fullmatch(str(plan_id or "")):
        raise GuangYaRenamePlanError("重命名计划编号无效")
    return _plan_directory() / f"{plan_id}.json"


def _journal_path(plan_id: str) -> Path:
    if not _SAFE_PLAN_ID.fullmatch(str(plan_id or "")):
        raise GuangYaRenamePlanError("重命名计划编号无效")
    return _plan_directory() / f"{plan_id}.jsonl"


def _canonical_payload(payload: dict[str, Any]) -> bytes:
    safe = {key: value for key, value in payload.items() if key != "auth"}
    return json.dumps(
        safe, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")


def _plan_auth(payload: dict[str, Any]) -> str:
    secret = str(get_web_secret() or "").encode("utf-8")
    if not secret:
        raise GuangYaRenamePlanError("重命名计划签名密钥不可用")
    return hmac.new(
        secret, b"mediaflux-guangya-rename-plan:v1\0" + _canonical_payload(payload),
        hashlib.sha256,
    ).hexdigest()


def _atomic_write_plan(path: Path, payload: dict[str, Any]) -> None:
    _ensure_private_directory(path.parent)
    stored = dict(payload)
    stored["auth"] = _plan_auth(stored)
    encoded = (
        json.dumps(stored, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    if len(encoded) > _MAX_PLAN_FILE_BYTES:
        raise GuangYaRenamePlanError("重命名计划过大，请缩小路径范围或降低 limit")
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent,
    )
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
            raise GuangYaRenamePlanError("重命名计划文件权限不安全")
    except BaseException:
        try:
            os.unlink(path if replaced else temporary)
        except FileNotFoundError:
            pass
        raise


def _append_journal(plan_id: str, event: dict[str, Any]) -> None:
    path = _journal_path(plan_id)
    _ensure_private_directory(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(
                {"at": _now_iso(), **event}, ensure_ascii=False, separators=(",", ":"),
            ) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        protect_private_file(path)


def _read_plan(plan_id: str) -> dict[str, Any]:
    path = _plan_path(plan_id)
    try:
        if path.is_symlink() or not path.is_file():
            raise GuangYaRenamePlanError("重命名计划不存在或已过期")
        raw = path.read_text(encoding="utf-8")
        if len(raw.encode("utf-8")) > _MAX_PLAN_FILE_BYTES:
            raise GuangYaRenamePlanError("重命名计划文件异常")
        payload = json.loads(raw)
    except FileNotFoundError as exc:
        raise GuangYaRenamePlanError("重命名计划不存在或已过期") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GuangYaRenamePlanError("重命名计划文件损坏") from exc
    if not isinstance(payload, dict) or int(payload.get("version") or 0) != _PLAN_VERSION:
        raise GuangYaRenamePlanError("重命名计划版本无效")
    actual = str(payload.get("auth") or "")
    expected = _plan_auth(payload)
    if not actual or not hmac.compare_digest(actual, expected):
        raise GuangYaRenamePlanError("重命名计划完整性校验失败")
    return payload


def _owner_digest(owner: str) -> str:
    owner_key = str(owner or "").strip()
    if not owner_key:
        raise GuangYaRenamePlanError("重命名计划缺少会话身份")
    return organize_operation_owner_digest(owner_key)


def load_rename_plan(
    plan_id: str,
    *,
    owner: str | None = None,
    expected_fingerprint: str = "",
    require_confirmed: bool = False,
) -> dict[str, Any]:
    payload = _read_plan(plan_id)
    if owner is not None and not hmac.compare_digest(
        str(payload.get("owner_digest") or ""), _owner_digest(owner),
    ):
        raise GuangYaRenamePlanError("重命名计划不属于当前会话")
    fingerprint = str(payload.get("fingerprint") or "")
    if expected_fingerprint and not hmac.compare_digest(
        fingerprint, str(expected_fingerprint or ""),
    ):
        raise GuangYaRenamePlanStale("重命名计划已变化，请重新预览")
    now_epoch = time.time()
    if require_confirmed:
        confirmed_at = float(payload.get("confirmed_at_epoch") or 0)
        execute_until = float(payload.get("execute_until_epoch") or 0)
        if confirmed_at <= 0 or execute_until <= now_epoch:
            raise GuangYaRenamePlanStale("重命名确认已过期，请重新预览")
    elif float(payload.get("expires_at_epoch") or 0) <= now_epoch:
        raise GuangYaRenamePlanStale("重命名预览已过期，请重新生成")
    return payload


def confirm_rename_plan(
    plan_id: str, *, owner: str, expected_fingerprint: str,
) -> dict[str, Any]:
    payload = load_rename_plan(
        plan_id, owner=owner, expected_fingerprint=expected_fingerprint,
    )
    current = time.time()
    payload["confirmed_at"] = _now_iso()
    payload["confirmed_at_epoch"] = current
    payload["execute_until_epoch"] = current + _CONFIRMED_TTL_SECONDS
    payload["status"] = "confirmed"
    _atomic_write_plan(_plan_path(plan_id), payload)
    return payload


def update_rename_plan_execution(
    plan_id: str, *, status: str, execution: dict[str, Any],
) -> None:
    payload = _read_plan(plan_id)
    payload["status"] = str(status or "unknown")[:40]
    payload["execution"] = dict(execution)
    payload["updated_at"] = _now_iso()
    _atomic_write_plan(_plan_path(plan_id), payload)


def discard_rename_plan(plan_id: str) -> None:
    """删除尚未执行或已被新预览取代的私有计划与日志。"""
    for path in (_plan_path(plan_id), _journal_path(plan_id)):
        try:
            if path.is_file() and not path.is_symlink():
                path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            continue


def _normalize_path(value: object) -> str:
    path = str(value or "").strip().replace("\\", "/")
    if not path.startswith("/") or len(path) > 2048:
        raise GuangYaRenamePlanError("光鸭路径必须是绝对路径")
    parts = [part for part in path.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise GuangYaRenamePlanError("不能重命名光鸭根目录")
    if any(len(part) > _MAX_NAME_LENGTH for part in parts):
        raise GuangYaRenamePlanError("光鸭路径组件过长")
    return "/" + "/".join(parts)


def _validate_name(value: object) -> str:
    name = str(value or "")
    if not name or name != name.strip() or len(name) > _MAX_NAME_LENGTH:
        raise GuangYaRenamePlanError("目标名称为空、过长或包含首尾空格")
    if name in {".", ".."} or any(char in name for char in ("/", "\\", "\0")):
        raise GuangYaRenamePlanError("目标名称包含不允许的字符")
    if any(ord(char) < 32 for char in name):
        raise GuangYaRenamePlanError("目标名称包含控制字符")
    return name


def remove_legacy_bitrate(name: str) -> str:
    """删除紧凑或旧式空格 Mbps 字段，不误伤 H.264/H.265。"""
    # 先处理带空格的完整字段，避免紧凑规则把 ``2.4 Mbps`` 的
    # 小数部分误当成独立的 ``.4Mbps``。
    updated = _DASH_BITRATE_RE.sub("", str(name or ""))
    updated = _SPACED_BITRATE_RE.sub("", updated)
    updated = _COMPACT_BITRATE_RE.sub("", updated)
    return updated


def _extension(name: str) -> str:
    match = re.search(r"[.。．]([A-Za-z0-9]{1,10})$", str(name or ""))
    return match.group(1).casefold() if match else ""


def _item_extension(item: GuangYaFile) -> str:
    declared = str(item.extension or "").strip().lstrip(".").casefold()
    return declared or _extension(item.name)


def _transformed_name(
    item: GuangYaFile,
    *,
    mode: str,
    new_name: str = "",
    find_text: str = "",
    replace_text: str = "",
) -> str:
    if mode == "exact":
        candidate = _validate_name(new_name)
    elif mode == "remove_bitrate":
        candidate = _validate_name(remove_legacy_bitrate(item.name))
    elif mode == "replace_text":
        if find_text not in item.name:
            return item.name
        candidate = _validate_name(item.name.replace(find_text, replace_text))
    else:
        raise GuangYaRenamePlanError("不支持的重命名方式")
    if not item.is_dir and _extension(candidate) != _item_extension(item):
        raise GuangYaRenamePlanError("批量重命名不允许修改文件扩展名")
    return candidate


def _cached_list_dir(
    client: GuangYaClient,
    cache: dict[str, list[GuangYaFile]],
    parent_id: str,
) -> list[GuangYaFile]:
    key = str(parent_id)
    if key not in cache:
        cache[key] = client.list_dir(key)
    return cache[key]


def _resolve_path(
    client: GuangYaClient, path: str,
    cache: dict[str, list[GuangYaFile]],
) -> tuple[GuangYaFile, str]:
    normalized = _normalize_path(path)
    parent_id = "0"
    parent_path = ""
    for index, component in enumerate(normalized.strip("/").split("/")):
        items = _cached_list_dir(client, cache, parent_id)
        exact = [item for item in items if item.name == component]
        matches = exact or [item for item in items if item.name.casefold() == component.casefold()]
        if len(matches) != 1:
            raise GuangYaRenamePlanError(
                f"路径不存在或名称不唯一：/{'/'.join(normalized.strip('/').split('/')[:index + 1])}"
            )
        current = matches[0]
        if index < len(normalized.strip("/").split("/")) - 1 and not current.is_dir:
            raise GuangYaRenamePlanError("光鸭路径的中间组件不是目录")
        parent_path = "/" + "/".join(normalized.strip("/").split("/")[:index])
        parent_id = str(current.file_id)
    return current, parent_path or "/"


def _entry(item: GuangYaFile, *, parent_path: str, new_name: str) -> dict[str, Any]:
    old_path = (parent_path.rstrip("/") + "/" + item.name) if parent_path != "/" else "/" + item.name
    new_path = (parent_path.rstrip("/") + "/" + new_name) if parent_path != "/" else "/" + new_name
    return {
        "file_id": str(item.file_id),
        "parent_id": str(item.parent_id),
        "old_name": item.name,
        "new_name": new_name,
        "old_path": old_path,
        "new_path": new_path,
        "is_dir": bool(item.is_dir),
        "size": max(0, int(item.size or 0)),
        "etag": str(item.etag or ""),
        "extension": str(item.extension or _extension(item.name)).casefold(),
    }


def _fingerprint(payload: dict[str, Any]) -> str:
    selected = {
        "version": _PLAN_VERSION,
        "owner_digest": payload["owner_digest"],
        "credential_generation": payload["credential_generation"],
        "mode": payload["mode"],
        "recursive": payload["recursive"],
        "targets": payload["targets"],
        "entries": payload["entries"],
        "transform": payload.get("transform") or {},
    }
    return hashlib.sha256(json.dumps(
        selected, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")).hexdigest()


def maintain_rename_plans() -> dict[str, int]:
    """主动清理过期/超额私有计划，只淘汰不会再执行的终态计划。"""
    directory = _plan_directory()
    if not directory.exists() or directory.is_symlink():
        return {"removed": 0, "remaining": 0, "active": 0, "bytes": 0}
    current = time.time()
    removed = 0
    terminal: list[tuple[float, str, int]] = []
    total_bytes = 0
    active = 0
    plan_ids: set[str] = set()
    for path in directory.glob("*.json"):
        try:
            if not path.is_file() or path.is_symlink():
                continue
            plan_ids.add(path.stem)
            plan_size = max(0, int(path.stat().st_size))
            journal = _journal_path(path.stem)
            journal_size = (
                max(0, int(journal.stat().st_size))
                if journal.is_file() and not journal.is_symlink() else 0
            )
            stored_size = plan_size + journal_size
            try:
                payload = _read_plan(path.stem)
            except GuangYaRenamePlanError:
                payload = {}
            status = str(payload.get("status") or "").strip().casefold()
            updated_epoch = max(
                float(payload.get("created_at_epoch") or 0),
                float(path.stat().st_mtime or 0),
            )
            remove = False
            if status == "preview":
                remove = float(payload.get("expires_at_epoch") or 0) <= current
            elif status == "confirmed":
                remove = float(payload.get("execute_until_epoch") or 0) <= current
            elif status == "manual_review":
                remove = updated_epoch <= current - _MANUAL_REVIEW_RETENTION_SECONDS
                if not remove:
                    terminal.append((updated_epoch, path.stem, stored_size))
            elif status in {"completed", "partial", "failed", "cancelled"}:
                remove = updated_epoch <= current - _TERMINAL_RETENTION_SECONDS
                if not remove:
                    terminal.append((updated_epoch, path.stem, stored_size))
            elif not status and updated_epoch <= current - _TERMINAL_RETENTION_SECONDS:
                remove = True
            if remove:
                discard_rename_plan(path.stem)
                plan_ids.discard(path.stem)
                removed += 1
            else:
                total_bytes += stored_size
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

    terminal.sort(key=lambda item: (item[0], item[1]))
    while terminal and (
        len(terminal) > _MAX_TERMINAL_PLANS
        or total_bytes > _MAX_PLAN_STORAGE_BYTES
    ):
        _updated, plan_id, stored_size = terminal.pop(0)
        discard_rename_plan(plan_id)
        total_bytes = max(0, total_bytes - stored_size)
        removed += 1
    remaining = sum(
        1 for path in directory.glob("*.json")
        if path.is_file() and not path.is_symlink()
    )
    return {
        "removed": removed, "remaining": remaining,
        "active": active, "bytes": total_bytes,
    }


def _finalize_rename_plan(
    client: GuangYaClient,
    *,
    owner: str,
    mode: str,
    recursive: bool,
    targets: list[str],
    limit: int,
    cache: dict[str, list[GuangYaFile]],
    raw_entries: list[dict[str, Any]],
    scanned_items: int,
    scanned_dirs: int,
    no_change: int,
    transform: dict[str, str] | None = None,
    extra_stats: dict[str, int] | None = None,
) -> dict[str, Any]:
    by_target: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entry in raw_entries:
        by_target.setdefault(
            (entry["parent_id"], entry["new_name"].casefold()), [],
        ).append(entry)

    conflicts: list[dict[str, Any]] = []
    safe_entries: list[dict[str, Any]] = []
    selected_ids = {str(entry["file_id"]) for entry in raw_entries}
    for group in by_target.values():
        first = group[0]
        reason = ""
        if len(group) > 1:
            reason = "多个文件会映射到同一目标名称"
        else:
            siblings = _cached_list_dir(
                client, cache, str(first["parent_id"]),
            )
            occupied = [
                sibling for sibling in siblings
                if sibling.name.casefold() == first["new_name"].casefold()
                and str(sibling.file_id) != first["file_id"]
            ]
            if occupied:
                reason = (
                    "目标名称已存在"
                    if str(occupied[0].file_id) not in selected_ids
                    else "目标名称与同批次其他文件冲突"
                )
        if reason:
            conflicts.extend({**entry, "reason": reason} for entry in group)
        else:
            safe_entries.extend(group)

    summary = (
        f"找到 {len(safe_entries)} 个可安全重命名对象"
        if safe_entries else "没有可安全执行的名称变更"
    )
    extension_counts = Counter(
        entry["extension"] or "directory" for entry in safe_entries
    )
    plan_id = uuid.uuid4().hex
    current = time.time()
    stats = {
        "scanned_items": max(0, int(scanned_items)),
        "scanned_dirs": max(0, int(scanned_dirs)),
        "matched": len(raw_entries),
        "rename_count": len(safe_entries),
        "conflict_count": len(conflicts),
        "no_change_count": max(0, int(no_change)),
    }
    for key, value in (extra_stats or {}).items():
        stats[str(key)] = max(0, int(value or 0))
    payload: dict[str, Any] = {
        "version": _PLAN_VERSION,
        "plan_id": plan_id,
        "owner_digest": _owner_digest(owner),
        "created_at": _now_iso(),
        "created_at_epoch": current,
        "expires_at_epoch": current + _PLAN_TTL_SECONDS,
        "status": "preview",
        "credential_generation": int(client.credential_generation),
        "mode": str(mode or "").strip().casefold(),
        "recursive": bool(recursive),
        "targets": list(targets),
        "limit": max(1, min(int(limit), _MAX_RENAMES)),
        "summary": summary,
        "stats": stats,
        "extension_counts": dict(sorted(extension_counts.items())),
        "entries": sorted(
            safe_entries,
            key=lambda entry: (entry["old_path"].casefold(), entry["file_id"]),
        ),
        "conflicts": sorted(
            conflicts,
            key=lambda entry: (entry["old_path"].casefold(), entry["file_id"]),
        ),
        "samples": [
            {"before": item["old_name"], "after": item["new_name"]}
            for item in sorted(
                safe_entries, key=lambda entry: entry["old_path"].casefold()
            )[:5]
        ],
        "transform": dict(transform or {}),
        "rollback": {
            "available": bool(safe_entries),
            "basis": "file_id,parent_id,old_name,new_name,size,etag",
        },
    }
    payload["fingerprint"] = _fingerprint(payload)
    _atomic_write_plan(_plan_path(plan_id), payload)
    capacity = maintain_rename_plans()
    if (
        int(capacity.get("active") or 0) > _MAX_ACTIVE_PLANS
        or int(capacity.get("bytes") or 0) > _MAX_PLAN_STORAGE_BYTES
    ):
        discard_rename_plan(plan_id)
        raise GuangYaRenamePlanError("私有重命名计划空间已满，请稍后重试")
    return payload


def build_explicit_rename_plan(
    client: GuangYaClient,
    *,
    owner: str,
    target: str,
    changes: list[tuple[GuangYaFile, str, str]],
    cache: dict[str, list[GuangYaFile]],
    scanned_items: int,
    scanned_dirs: int,
    no_change: int = 0,
    limit: int = 1_000,
    mode: str = "media_hygiene",
    extra_stats: dict[str, int] | None = None,
    transform: dict[str, str] | None = None,
) -> dict[str, Any]:
    """把内部扫描器产生的精确名称映射冻结为通用重命名计划。"""
    normalized_target = _normalize_path(target)
    safe_mode = str(mode or "").strip().casefold()
    if safe_mode not in {"media_hygiene", "declarative"}:
        raise GuangYaRenamePlanError("显式重命名计划类型无效")
    safe_limit = max(1, min(int(limit), _MAX_RENAMES))
    if len(changes) > safe_limit:
        raise GuangYaRenamePlanError(
            f"匹配文件超过本次上限 {safe_limit} 个，请缩小范围或提高 limit"
        )
    raw_entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item, parent_path, proposed_name in changes:
        file_id = str(item.file_id or "").strip()
        if not file_id or file_id in seen_ids:
            raise GuangYaRenamePlanError("显式重命名计划包含重复或无效对象")
        candidate = _validate_name(proposed_name)
        if not item.is_dir and _extension(candidate) != _item_extension(item):
            raise GuangYaRenamePlanError("媒体名称清理不允许修改文件扩展名")
        if candidate == item.name:
            continue
        seen_ids.add(file_id)
        raw_entries.append(_entry(item, parent_path=parent_path, new_name=candidate))
    return _finalize_rename_plan(
        client,
        owner=owner,
        mode=safe_mode,
        recursive=True,
        targets=[normalized_target],
        limit=safe_limit,
        cache=cache,
        raw_entries=raw_entries,
        scanned_items=scanned_items,
        scanned_dirs=scanned_dirs,
        no_change=no_change,
        transform={
            "strategy": (
                "nsfw_media_hygiene"
                if safe_mode == "media_hygiene" else "llm_declarative_rename"
            ),
            **dict(transform or {}),
        },
        extra_stats=extra_stats,
    )


def build_rename_plan(
    client: GuangYaClient,
    *,
    owner: str,
    targets: list[str],
    mode: str,
    recursive: bool = False,
    limit: int = 100,
    new_name: str = "",
    find_text: str = "",
    replace_text: str = "",
) -> dict[str, Any]:
    """扫描并冻结一个不含目标冲突的重命名计划。"""
    normalized_targets = list(dict.fromkeys(_normalize_path(item) for item in targets))
    if not normalized_targets or len(normalized_targets) > _MAX_TARGETS:
        raise GuangYaRenamePlanError(f"一次只能指定 1 到 {_MAX_TARGETS} 个光鸭路径")
    safe_mode = str(mode or "").strip().casefold()
    if safe_mode not in {"exact", "remove_bitrate", "replace_text"}:
        raise GuangYaRenamePlanError("重命名方式无效")
    safe_limit = max(1, min(int(limit), _MAX_RENAMES))
    if safe_mode == "exact" and len(normalized_targets) != 1:
        raise GuangYaRenamePlanError("精确改名一次只能指定一个路径")
    if safe_mode == "exact" and recursive:
        raise GuangYaRenamePlanError("精确改名不支持递归")
    if safe_mode == "exact":
        _validate_name(new_name)
    if safe_mode == "replace_text":
        if not find_text or len(find_text) > 120 or len(replace_text) > 120:
            raise GuangYaRenamePlanError("文本替换参数无效")
        if find_text == replace_text:
            raise GuangYaRenamePlanError("替换前后文本相同")

    cache: dict[str, list[GuangYaFile]] = {}
    candidates: dict[str, dict[str, Any]] = {}
    no_change = 0
    scanned_items = 0
    scanned_dirs = 0

    def consider(item: GuangYaFile, parent_path: str) -> None:
        nonlocal no_change
        candidate = _transformed_name(
            item, mode=safe_mode, new_name=new_name,
            find_text=find_text, replace_text=replace_text,
        )
        if candidate == item.name:
            no_change += 1
            return
        candidates.setdefault(
            str(item.file_id), _entry(item, parent_path=parent_path, new_name=candidate),
        )
        if len(candidates) > safe_limit:
            raise GuangYaRenamePlanError(
                f"匹配文件超过本次上限 {safe_limit} 个，请缩小范围或提高 limit"
            )

    for target in normalized_targets:
        item, parent_path = _resolve_path(client, target, cache)
        if safe_mode == "exact":
            consider(item, parent_path)
            continue
        if not item.is_dir:
            consider(item, parent_path)
            continue
        queue = deque([(str(item.file_id), target)])
        seen_dirs: set[str] = set()
        while queue:
            directory_id, directory_path = queue.popleft()
            if directory_id in seen_dirs:
                continue
            seen_dirs.add(directory_id)
            scanned_dirs += 1
            if scanned_dirs > _MAX_SCANNED_DIRS:
                raise GuangYaRenamePlanError("递归目录数量超过安全上限")
            items = _cached_list_dir(client, cache, directory_id)
            for child in items:
                scanned_items += 1
                if scanned_items > _MAX_SCANNED_ITEMS:
                    raise GuangYaRenamePlanError("扫描项目数量超过安全上限")
                if child.is_dir:
                    if recursive:
                        queue.append((str(child.file_id), directory_path.rstrip("/") + "/" + child.name))
                    continue
                consider(child, directory_path)

    return _finalize_rename_plan(
        client,
        owner=owner,
        mode=safe_mode,
        recursive=recursive,
        targets=normalized_targets,
        limit=safe_limit,
        cache=cache,
        raw_entries=list(candidates.values()),
        scanned_items=scanned_items,
        scanned_dirs=scanned_dirs,
        no_change=no_change,
        transform={
            "new_name": new_name if safe_mode == "exact" else "",
            "find_text": find_text if safe_mode == "replace_text" else "",
            "replace_text": replace_text if safe_mode == "replace_text" else "",
        },
    )



def _snapshot_matches(item: GuangYaFile | None, entry: dict[str, Any], *, new: bool) -> bool:
    expected_name = entry["new_name"] if new else entry["old_name"]
    return bool(
        item is not None
        and str(item.file_id) == str(entry["file_id"])
        and item.name == expected_name
        and bool(item.is_dir) == bool(entry["is_dir"])
        and max(0, int(item.size or 0)) == max(0, int(entry["size"] or 0))
        and str(item.etag or "") == str(entry.get("etag") or "")
    )


def execute_rename_plan(
    payload: dict[str, Any],
    *,
    cancel_check: Callable[[], None] | None = None,
    client_factory: Callable[[], GuangYaClient] = GuangYaClient,
) -> dict[str, Any]:
    """执行持久任务载荷；返回只含聚合计数的整理结果。"""
    if not isinstance(payload, dict) or int(payload.get("version") or 0) != 1:
        raise GuangYaRenamePlanError("重命名任务参数无效")
    plan_id = str(payload.get("plan_id") or "")
    expected_fingerprint = str(payload.get("plan_fingerprint") or "")
    plan = load_rename_plan(
        plan_id, expected_fingerprint=expected_fingerprint, require_confirmed=True,
    )
    if not hmac.compare_digest(
        str(plan.get("owner_digest") or ""), str(payload.get("owner_digest") or ""),
    ):
        raise GuangYaRenamePlanError("重命名任务会话不匹配")
    expected_generation = int(payload.get("credential_generation") or -1)
    client = client_factory()
    renamed = 0
    failed = 0
    provider_rejected = 0
    verification_failed = 0
    journal_started = False
    started_at = _now_iso()
    try:
        if not client.logged_in or int(client.credential_generation) != expected_generation:
            raise GuangYaRenamePlanStale("光鸭登录凭据已变化，请重新预览")
        entries = list(plan.get("entries") or [])
        if not entries:
            raise GuangYaRenamePlanError("重命名计划没有可执行对象")
        parent_snapshots: dict[str, dict[str, GuangYaFile]] = {}
        for entry in entries:
            if cancel_check is not None:
                cancel_check()
            parent_id = str(entry["parent_id"])
            if parent_id not in parent_snapshots:
                parent_snapshots[parent_id] = {
                    str(item.file_id): item for item in client.list_dir(parent_id)
                }
            current = parent_snapshots[parent_id].get(str(entry["file_id"]))
            if not _snapshot_matches(current, entry, new=False):
                raise GuangYaRenamePlanStale("文件名称或内容已变化，请重新预览")
            conflicts = [
                item for item in parent_snapshots[parent_id].values()
                if item.name.casefold() == str(entry["new_name"]).casefold()
                and str(item.file_id) != str(entry["file_id"])
            ]
            if conflicts:
                raise GuangYaRenamePlanStale("目标名称已被占用，请重新预览")

        update_rename_plan_execution(plan_id, status="running", execution={
            "started_at": started_at,
            "total": len(entries),
            "renamed": 0,
            "failed": 0,
            "journal": _journal_path(plan_id).name,
        })
        _append_journal(plan_id, {"action": "preflight", "status": "completed", "total": len(entries)})
        journal_started = True

        precondition_failed = 0
        for index, entry in enumerate(entries, start=1):
            if cancel_check is not None:
                cancel_check()
            error_type = ""
            provider_code = ""
            # 大批量任务可能执行较久；每一项写入前再次核对源对象，避免
            # 预检之后发生的内容或名称变化被后续重命名覆盖。
            current_before = None
            try:
                current_before = client.file_info(str(entry["file_id"]))
            except Exception:
                current_before = None
            if not _snapshot_matches(current_before, entry, new=False):
                try:
                    current_before = {
                        str(item.file_id): item
                        for item in client.list_dir(str(entry["parent_id"]))
                    }.get(str(entry["file_id"]))
                except Exception:
                    current_before = None
            if not _snapshot_matches(current_before, entry, new=False):
                failed += 1
                precondition_failed += 1
                _append_journal(plan_id, {
                    "action": "rename",
                    "index": index,
                    "file_id": str(entry["file_id"]),
                    "parent_id": str(entry["parent_id"]),
                    "old_name": str(entry["old_name"]),
                    "new_name": str(entry["new_name"]),
                    "status": "failed",
                    "error_type": "PreWriteSnapshotChanged",
                    "provider_code": "",
                })
                continue
            try:
                client.rename(str(entry["file_id"]), str(entry["new_name"]))
            except GuangYaWriteRejected as exc:
                error_type = type(exc).__name__
                provider_code = exc.code
                provider_rejected += 1
            except Exception as exc:
                error_type = type(exc).__name__
            # HTTP/SDK 结果不是成功凭据；始终按 file_id 重新读取真实名称。
            current = None
            try:
                current = client.file_info(str(entry["file_id"]))
            except Exception:
                current = None
            if not _snapshot_matches(current, entry, new=True):
                try:
                    current = {
                        str(item.file_id): item
                        for item in client.list_dir(str(entry["parent_id"]))
                    }.get(str(entry["file_id"]))
                except Exception:
                    current = None
            applied = _snapshot_matches(current, entry, new=True)
            if applied:
                renamed += 1
            else:
                failed += 1
                if not error_type:
                    verification_failed += 1
                    error_type = "PostWriteVerificationFailed"
            _append_journal(plan_id, {
                "action": "rename",
                "index": index,
                "file_id": str(entry["file_id"]),
                "parent_id": str(entry["parent_id"]),
                "old_name": str(entry["old_name"]),
                "new_name": str(entry["new_name"]),
                "status": "completed" if applied else "failed",
                "error_type": error_type,
                "provider_code": provider_code,
            })
            time.sleep(0.12)

        status = "completed" if failed == 0 else "partial"
        execution = {
            "started_at": started_at,
            "finished_at": _now_iso(),
            "total": len(entries),
            "renamed": renamed,
            "failed": failed,
            "provider_rejected": provider_rejected,
            "precondition_failed": precondition_failed,
            "verification_failed": verification_failed,
            "journal": _journal_path(plan_id).name,
        }
        update_rename_plan_execution(plan_id, status=status, execution=execution)
        _append_journal(plan_id, {"action": "final", "status": status, **execution})
        return {
            "partial": failed > 0,
            "stats": {
                "total": len(entries),
                "renamed": renamed,
                "rename_failed": failed,
                "failed": failed,
            },
        }
    except BaseException as exc:
        if journal_started:
            _append_journal(plan_id, {
                "action": "fatal", "status": "failed", "error_type": type(exc).__name__,
            })
        try:
            update_rename_plan_execution(plan_id, status="failed", execution={
                "started_at": started_at,
                "finished_at": _now_iso(),
                "renamed": renamed,
                "failed": failed,
                "error_type": type(exc).__name__,
                "journal": _journal_path(plan_id).name if journal_started else "",
            })
        except Exception:
            pass
        raise
    finally:
        client.close()
        try:
            maintain_rename_plans()
        except Exception:
            pass
