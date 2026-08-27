"""光鸭声明式工作区的私有只读观察快照。"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from app.clients.guangya import GuangYaClient, GuangYaFile
from app.config import PATHS
from app.modules.organize import DEFAULT_ORGANIZE_METADATA_EXTS, DEFAULT_ORGANIZE_VIDEO_EXTS
from app.modules.organize_postprocess import SUBTITLE_EXTS
from app.modules.web_secret import get_web_secret
from app.private_files import protect_private_file
from app.repositories.organize_operation_jobs import organize_operation_owner_digest

_OBSERVATION_VERSION = 1
_OBSERVATION_TTL_SECONDS = 10 * 60
_MAX_SCANNED_ITEMS = 2_000
_MAX_SCANNED_DIRS = 500
_MAX_DEPTH = 12
_MAX_PLAN_BYTES = 8 * 1024 * 1024
_MAX_STORAGE_BYTES = 64 * 1024 * 1024
_MAX_OBSERVATIONS = 32
_MAX_OBSERVATIONS_PER_OWNER = 4
_SAFE_PLAN_ID = re.compile(r"^[0-9a-f]{32}$")
_SAFE_OBSERVATION_REF = re.compile(r"^OBS[0-9A-F]{32}$")
_SAFE_HANDLE = re.compile(r"^OBJ[0-9A-F]{24}$")
_IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "avif"}


class GuangYaWorkspaceError(RuntimeError):
    """可安全映射给 Agent 的光鸭工作区错误。"""


class GuangYaWorkspaceStale(GuangYaWorkspaceError):
    """观察快照已过期或不再属于当前会话。"""


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _directory() -> Path:
    return Path(PATHS.data_dir) / "agent-guangya-workspace"


def _ensure_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        if path.is_symlink() or not path.is_dir():
            raise GuangYaWorkspaceError("光鸭观察快照目录不可用")
        if os.name == "posix":
            path.chmod(0o700)
    except OSError as exc:
        raise GuangYaWorkspaceError("光鸭观察快照目录不可用") from exc


def _plan_path(plan_id: str) -> Path:
    if not _SAFE_PLAN_ID.fullmatch(str(plan_id or "")):
        raise GuangYaWorkspaceError("光鸭观察编号无效")
    return _directory() / f"{plan_id}.json"


def observation_ref(plan_id: str) -> str:
    if not _SAFE_PLAN_ID.fullmatch(str(plan_id or "")):
        raise GuangYaWorkspaceError("光鸭观察编号无效")
    return "OBS" + str(plan_id).upper()


def observation_plan_id(value: object) -> str:
    ref = str(value or "").strip().upper()
    if not _SAFE_OBSERVATION_REF.fullmatch(ref):
        raise GuangYaWorkspaceError("光鸭观察编号无效")
    return ref[3:].casefold()


def valid_observation_ref(value: object) -> bool:
    return bool(_SAFE_OBSERVATION_REF.fullmatch(str(value or "").strip().upper()))


def valid_object_handle(value: object) -> bool:
    return bool(_SAFE_HANDLE.fullmatch(str(value or "").strip().upper()))


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
        raise GuangYaWorkspaceError("光鸭观察签名密钥不可用")
    return hmac.new(
        secret, b"mediaflux-guangya-workspace:v1\0" + _canonical(payload),
        hashlib.sha256,
    ).hexdigest()


def _atomic_write(payload: dict[str, Any]) -> None:
    path = _plan_path(str(payload.get("plan_id") or ""))
    _ensure_directory(path.parent)
    stored = dict(payload)
    stored["auth"] = _auth(stored)
    encoded = (json.dumps(stored, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > _MAX_PLAN_BYTES:
        raise GuangYaWorkspaceError("光鸭观察范围过大，请缩小目录或关闭递归")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        protect_private_file(Path(temporary))
        os.replace(temporary, path)
        protect_private_file(path)
    finally:
        try:
            if os.path.exists(temporary):
                os.unlink(temporary)
        except OSError:
            pass


def _read(plan_id: str) -> dict[str, Any]:
    path = _plan_path(plan_id)
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_PLAN_BYTES:
            raise GuangYaWorkspaceError("光鸭观察快照不可用")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise GuangYaWorkspaceError("光鸭观察快照不可用") from exc
    if not isinstance(payload, dict) or int(payload.get("version") or 0) != _OBSERVATION_VERSION:
        raise GuangYaWorkspaceError("光鸭观察快照版本无效")
    actual = str(payload.get("auth") or "")
    if not actual or not hmac.compare_digest(actual, _auth(payload)):
        raise GuangYaWorkspaceError("光鸭观察快照校验失败")
    return payload


def discard_observation(value: object) -> None:
    try:
        plan_id = observation_plan_id(value) if str(value or "").upper().startswith("OBS") else str(value or "")
        path = _plan_path(plan_id)
        if path.is_file() and not path.is_symlink():
            path.unlink()
    except (OSError, GuangYaWorkspaceError):
        return


def maintain_workspace_observations(
    *, preserve_plan_id: str = "",
) -> dict[str, int]:
    directory = _directory()
    if not directory.exists() or directory.is_symlink():
        return {"removed": 0, "remaining": 0, "bytes": 0}
    preserved = str(preserve_plan_id or "").strip().casefold()
    current = time.time()
    rows: list[tuple[float, str, int, str]] = []
    removed = 0
    total_bytes = 0
    for path in directory.glob("*.json"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            stat = path.stat()
            size = max(0, int(stat.st_size))
            try:
                payload = _read(path.stem)
                expiry = float(payload.get("expires_at_epoch") or 0)
                created = float(payload.get("created_at_epoch") or stat.st_mtime)
                owner_digest = str(payload.get("owner_digest") or "")
            except GuangYaWorkspaceError:
                expiry = 0
                created = float(stat.st_mtime)
                owner_digest = ""
            if expiry <= current:
                path.unlink()
                removed += 1
                continue
            rows.append((created, path.stem, size, owner_digest))
            total_bytes += size
        except OSError:
            continue
    rows.sort()

    def remove_row(row: tuple[float, str, int, str]) -> None:
        nonlocal removed, total_bytes
        _created, plan_id, size, _owner_digest = row
        discard_observation(plan_id)
        try:
            rows.remove(row)
        except ValueError:
            return
        total_bytes = max(0, total_bytes - size)
        removed += 1

    owner_digests = {row[3] for row in rows if row[3]}
    for owner_digest in owner_digests:
        owner_rows = [row for row in rows if row[3] == owner_digest]
        while len(owner_rows) > _MAX_OBSERVATIONS_PER_OWNER:
            victim = next(
                (row for row in owner_rows if row[1] != preserved),
                None,
            )
            if victim is None:
                break
            remove_row(victim)
            owner_rows.remove(victim)

    while rows and (
        len(rows) > _MAX_OBSERVATIONS or total_bytes > _MAX_STORAGE_BYTES
    ):
        victim = next((row for row in rows if row[1] != preserved), None)
        if victim is None:
            break
        remove_row(victim)
    return {"removed": removed, "remaining": len(rows), "bytes": total_bytes}


def _normalize_path(value: object) -> str:
    path = str(value or "").strip().replace("\\", "/")
    if not path.startswith("/") or len(path) > 2048:
        raise GuangYaWorkspaceError("光鸭目录路径必须是绝对路径")
    parts = [part for part in path.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise GuangYaWorkspaceError("不能观察光鸭根目录或相对路径")
    return "/" + "/".join(parts)


def _resolve_path(client: GuangYaClient, path: str) -> tuple[GuangYaFile, str]:
    components = _normalize_path(path).strip("/").split("/")
    parent_id = "0"
    parent_path = "/"
    for index, component in enumerate(components):
        items = client.list_dir(parent_id)
        exact = [item for item in items if item.name == component]
        matches = exact or [item for item in items if item.name.casefold() == component.casefold()]
        if len(matches) != 1:
            raise GuangYaWorkspaceError(
                f"路径不存在或名称不唯一：/{'/'.join(components[:index + 1])}"
            )
        current = matches[0]
        if index < len(components) - 1 and not current.is_dir:
            raise GuangYaWorkspaceError("光鸭路径的中间组件不是目录")
        parent_path = "/" + "/".join(components[:index]) if index else "/"
        parent_id = str(current.file_id)
    return current, parent_path


def _extension(item: GuangYaFile) -> str:
    declared = str(item.extension or "").strip().lower().lstrip(".")
    if declared:
        return declared
    match = re.search(r"[.。．]([A-Za-z0-9]{1,10})$", item.name)
    return match.group(1).lower() if match else ""


def _media_kind(item: GuangYaFile) -> str:
    if item.is_dir:
        return "directory"
    ext = _extension(item)
    if ext in DEFAULT_ORGANIZE_VIDEO_EXTS:
        return "video"
    if ext in SUBTITLE_EXTS:
        return "subtitle"
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in DEFAULT_ORGANIZE_METADATA_EXTS:
        return "metadata"
    return "other"


def _snapshot(item: GuangYaFile, *, parent_path: str, relative_parent: str) -> dict[str, Any]:
    return {
        "file_id": str(item.file_id),
        "parent_id": str(item.parent_id),
        "name": str(item.name),
        "is_dir": bool(item.is_dir),
        "size": max(0, int(item.size or 0)),
        "etag": str(item.etag or ""),
        "updated_at": max(0, int(item.updated_at or 0)),
        "extension": _extension(item),
        "media_kind": _media_kind(item),
        "parent_path": parent_path,
        "relative_parent": relative_parent,
    }


def _handle(plan_id: str, item: dict[str, Any]) -> str:
    secret = str(get_web_secret() or "").encode("utf-8")
    if not secret:
        raise GuangYaWorkspaceError("光鸭观察签名密钥不可用")
    digest = hmac.new(
        secret,
        b"mediaflux-guangya-object-handle:v1\0"
        + plan_id.encode("ascii")
        + b"\0"
        + str(item.get("file_id") or "").encode("utf-8")
        + b"\0"
        + str(item.get("parent_id") or "").encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:24].upper()
    return "OBJ" + digest


def create_directory_observation(
    client: GuangYaClient,
    *,
    owner: str,
    path: str,
    recursive: bool = False,
    max_items: int = 500,
) -> dict[str, Any]:
    target_path = _normalize_path(path)
    safe_max = max(1, min(int(max_items), _MAX_SCANNED_ITEMS))
    target, _parent_path = _resolve_path(client, target_path)
    if not target.is_dir:
        raise GuangYaWorkspaceError("通用目录观察只支持精确目录路径")

    queue = deque([(str(target.file_id), target_path, "", 0)])
    entries: list[dict[str, Any]] = []
    scanned_dirs = 0
    truncated = False
    while queue:
        directory_id, directory_path, relative_parent, depth = queue.popleft()
        scanned_dirs += 1
        if scanned_dirs > _MAX_SCANNED_DIRS:
            truncated = True
            break
        for item in client.list_dir(directory_id):
            if len(entries) >= safe_max:
                truncated = True
                queue.clear()
                break
            entry = _snapshot(
                item,
                parent_path=directory_path,
                relative_parent=relative_parent or "当前目录",
            )
            entries.append(entry)
            if item.is_dir and recursive and depth < _MAX_DEPTH:
                child_path = directory_path.rstrip("/") + "/" + item.name
                child_relative = (
                    item.name if not relative_parent else f"{relative_parent} › {item.name}"
                )
                queue.append((str(item.file_id), child_path, child_relative, depth + 1))
            elif item.is_dir and recursive and depth >= _MAX_DEPTH:
                truncated = True

    entries.sort(key=lambda row: (
        str(row.get("relative_parent") or "").casefold(),
        not bool(row.get("is_dir")),
        str(row.get("name") or "").casefold(),
        str(row.get("file_id") or ""),
    ))
    plan_id = uuid.uuid4().hex
    for entry in entries:
        entry["handle"] = _handle(plan_id, entry)
    current = time.time()
    payload = {
        "version": _OBSERVATION_VERSION,
        "plan_id": plan_id,
        "owner_digest": organize_operation_owner_digest(owner),
        "credential_generation": int(client.credential_generation),
        "created_at": _now_iso(),
        "created_at_epoch": current,
        "expires_at_epoch": current + _OBSERVATION_TTL_SECONDS,
        "scope_path": target_path,
        "scope_name": str(target.name),
        "recursive": bool(recursive),
        "truncated": bool(truncated),
        "scanned_dirs": scanned_dirs,
        "entries": entries,
    }
    _atomic_write(payload)
    maintain_workspace_observations(preserve_plan_id=plan_id)
    if not _plan_path(plan_id).exists():
        raise GuangYaWorkspaceError("光鸭观察快照容量已满，请稍后重试")
    return payload


def load_directory_observation(
    value: object,
    *,
    owner: str,
    require_fresh: bool = True,
) -> dict[str, Any]:
    plan_id = observation_plan_id(value)
    payload = _read(plan_id)
    if not hmac.compare_digest(
        str(payload.get("owner_digest") or ""), organize_operation_owner_digest(owner)
    ):
        raise GuangYaWorkspaceError("光鸭观察快照不属于当前会话")
    if require_fresh and float(payload.get("expires_at_epoch") or 0) <= time.time():
        discard_observation(plan_id)
        raise GuangYaWorkspaceStale("光鸭目录观察已过期，请重新读取")
    return payload


def observation_page(
    payload: dict[str, Any], *, page: int = 1, page_size: int = 10
) -> dict[str, Any]:
    safe_size = max(1, min(int(page_size), 10))
    safe_page = max(1, int(page))
    entries = list(payload.get("entries") or [])
    start = (safe_page - 1) * safe_size
    selected = entries[start:start + safe_size]
    public_entries = [
        {
            "object_ref": str(item.get("handle") or ""),
            "object_name": str(item.get("name") or "")[:255],
            "kind": str(item.get("media_kind") or "other"),
            "extension": str(item.get("extension") or ""),
            "size": max(0, int(item.get("size") or 0)),
            "location": str(item.get("relative_parent") or "当前目录")[:240],
        }
        for item in selected
    ]
    return {
        "observation_ref": observation_ref(str(payload.get("plan_id") or "")),
        "scope": str(payload.get("scope_name") or "当前目录")[:120],
        "recursive": bool(payload.get("recursive")),
        "page": safe_page,
        "page_size": safe_size,
        "total": len(entries),
        "has_more": start + len(selected) < len(entries),
        "truncated": bool(payload.get("truncated")),
        "entries": public_entries,
        "cloud_write": False,
    }


def observation_entry_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in payload.get("entries") or []:
        if not isinstance(raw, dict):
            continue
        handle = str(raw.get("handle") or "").strip().upper()
        if valid_object_handle(handle) and handle not in result:
            result[handle] = dict(raw)
    return result


def _matches_observation(item: GuangYaFile | None, snapshot: dict[str, Any]) -> bool:
    return bool(
        item is not None
        and str(item.file_id) == str(snapshot.get("file_id") or "")
        and str(item.name) == str(snapshot.get("name") or "")
        and bool(item.is_dir) == bool(snapshot.get("is_dir"))
        and max(0, int(item.size or 0)) == max(0, int(snapshot.get("size") or 0))
        and str(item.etag or "") == str(snapshot.get("etag") or "")
    )


def build_declarative_rename_plan(
    client: GuangYaClient,
    *,
    owner: str,
    observation: dict[str, Any],
    operations: list[dict[str, str]],
    trigger_strm: bool = True,
) -> dict[str, Any]:
    """把 LLM 提议的对象引用改名映射编译为通用冻结重命名计划。"""
    from app.modules.guangya_rename import (
        GuangYaRenamePlanStale,
        build_explicit_rename_plan,
    )

    if not isinstance(observation, dict):
        raise GuangYaWorkspaceError("光鸭观察快照格式无效")
    try:
        version = int(observation.get("version") or 0)
        expiry = float(observation.get("expires_at_epoch") or 0)
        credential_generation = int(
            observation.get("credential_generation") or -1
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise GuangYaWorkspaceError("光鸭观察快照字段格式无效") from exc
    if version != _OBSERVATION_VERSION:
        raise GuangYaWorkspaceError("光鸭观察快照版本无效")
    plan_id = str(observation.get("plan_id") or "")
    if not _SAFE_PLAN_ID.fullmatch(plan_id):
        raise GuangYaWorkspaceError("光鸭观察编号无效")
    if not hmac.compare_digest(
        str(observation.get("owner_digest") or ""),
        organize_operation_owner_digest(owner),
    ):
        raise GuangYaWorkspaceError("光鸭观察快照不属于当前会话")
    if expiry <= time.time():
        raise GuangYaWorkspaceStale("光鸭目录观察已过期，请重新读取")
    entries = observation.get("entries")
    if not isinstance(entries, list):
        raise GuangYaWorkspaceError("光鸭观察快照条目格式无效")
    if int(client.credential_generation) != credential_generation:
        raise GuangYaWorkspaceStale("光鸭登录凭据已变化，请重新读取目录")
    if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
        raise GuangYaWorkspaceError("声明式改名计划必须包含 1 到 100 项操作")
    entry_map = observation_entry_map(observation)
    parent_cache: dict[str, list[GuangYaFile]] = {}
    changes: list[tuple[GuangYaFile, str, str]] = []
    seen: set[str] = set()
    no_change = 0
    for operation in operations:
        if not isinstance(operation, dict):
            raise GuangYaWorkspaceError("声明式计划操作格式无效")
        handle = str(operation.get("handle") or "").strip().upper()
        if not valid_object_handle(handle) or handle not in entry_map:
            raise GuangYaWorkspaceError("声明式计划包含当前观察中不存在的对象引用")
        if handle in seen:
            raise GuangYaWorkspaceError("声明式计划不能重复操作同一个对象")
        seen.add(handle)
        snapshot = entry_map[handle]
        parent_id = str(snapshot.get("parent_id") or "0")
        if parent_id not in parent_cache:
            parent_cache[parent_id] = client.list_dir(parent_id)
        siblings = parent_cache[parent_id]
        current = {
            str(item.file_id): item for item in siblings
        }.get(str(snapshot.get("file_id") or ""))
        if not _matches_observation(current, snapshot):
            raise GuangYaWorkspaceStale("目录对象已变化，请重新读取后再生成计划")
        new_name = operation.get("new_name")
        if not isinstance(new_name, str):
            raise GuangYaWorkspaceError("声明式计划目标名称格式无效")
        if current is not None and new_name == current.name:
            no_change += 1
            continue
        if current is None:
            raise GuangYaRenamePlanStale("目录对象已变化，请重新读取")
        changes.append((
            current,
            str(snapshot.get("parent_path") or "/"),
            new_name,
        ))

    return build_explicit_rename_plan(
        client,
        owner=owner,
        target=str(observation.get("scope_path") or ""),
        changes=changes,
        cache=parent_cache,
        scanned_items=len(entries),
        scanned_dirs=max(0, int(observation.get("scanned_dirs") or 0)),
        no_change=no_change,
        limit=100,
        mode="declarative",
        extra_stats={"proposed_operation_count": len(operations)},
        transform={
            "observation_ref": observation_ref(
                plan_id
            ),
            "trigger_strm": "1" if trigger_strm else "0",
        },
    )
