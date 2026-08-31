"""光鸭文件系统通用变更的私有冻结计划与可验证执行器。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from app.clients.guangya import GuangYaClient, GuangYaFile, GuangYaWriteRejected
from app.config import PATHS
from app.modules.guangya_workspace import (
    GuangYaWorkspaceStale,
    observation_entry_map,
    observation_ref,
    resolve_workspace_path,
    valid_object_handle,
)
from app.modules.web_secret import get_web_secret
from app.private_files import protect_private_file
from app.repositories.organize_operation_jobs import organize_operation_owner_digest

_PLAN_VERSION = 1
_PLAN_TTL_SECONDS = 10 * 60
_EXECUTE_TTL_SECONDS = 15 * 60
_MAX_OPERATIONS = 50
_MAX_PLAN_BYTES = 2 * 1024 * 1024
_MAX_PLANS = 32
_MAX_PLANS_PER_OWNER = 4
_SAFE_PLAN_ID = re.compile(r"^[0-9a-f]{32}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class GuangYaFSChangeError(RuntimeError):
    """可安全映射给 Agent 的通用光鸭变更错误。"""


class GuangYaFSChangeStale(GuangYaFSChangeError):
    """冻结计划已过期或云端对象已变化。"""


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _directory() -> Path:
    return Path(PATHS.data_dir) / "agent-guangya-fs-change"


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        if path.is_symlink() or not path.is_dir():
            raise GuangYaFSChangeError("光鸭变更计划目录不可用")
        if os.name == "posix":
            path.chmod(0o700)
    except OSError as exc:
        raise GuangYaFSChangeError("光鸭变更计划目录不可用") from exc


def _plan_path(plan_id: str) -> Path:
    if not _SAFE_PLAN_ID.fullmatch(str(plan_id or "")):
        raise GuangYaFSChangeError("光鸭变更计划编号无效")
    return _directory() / f"{plan_id}.json"


def _journal_path(plan_id: str) -> Path:
    if not _SAFE_PLAN_ID.fullmatch(str(plan_id or "")):
        raise GuangYaFSChangeError("光鸭变更计划编号无效")
    return _directory() / f"{plan_id}.journal.jsonl"


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        {key: value for key, value in payload.items() if key != "auth"},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _secret() -> bytes:
    secret = str(get_web_secret() or "").encode("utf-8")
    if not secret:
        raise GuangYaFSChangeError("光鸭变更签名密钥不可用")
    return secret


def _auth(payload: dict[str, Any]) -> str:
    return hmac.new(
        _secret(),
        b"mediaflux-guangya-fs-change:v1\0" + _canonical(payload),
        hashlib.sha256,
    ).hexdigest()


def _fingerprint(payload: dict[str, Any]) -> str:
    stable = {
        "version": payload.get("version"),
        "plan_id": payload.get("plan_id"),
        "owner_digest": payload.get("owner_digest"),
        "credential_generation": payload.get("credential_generation"),
        "observation_ref": payload.get("observation_ref"),
        "trigger_strm": payload.get("trigger_strm"),
        "operations": payload.get("operations"),
    }
    return hmac.new(
        _secret(),
        b"mediaflux-guangya-fs-change-fingerprint:v1\0"
        + json.dumps(
            stable, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _atomic_write(payload: dict[str, Any]) -> None:
    path = _plan_path(str(payload.get("plan_id") or ""))
    _ensure_private_directory(path.parent)
    stored = dict(payload)
    stored["auth"] = _auth(stored)
    encoded = (
        json.dumps(stored, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if len(encoded) > _MAX_PLAN_BYTES:
        raise GuangYaFSChangeError("光鸭变更计划过大，请缩小操作范围")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    replaced = False
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        protect_private_file(Path(temporary))
        os.replace(temporary, path)
        replaced = True
        if not protect_private_file(path):
            raise GuangYaFSChangeError("光鸭变更计划文件权限不安全")
    except BaseException:
        try:
            os.unlink(path if replaced else temporary)
        except FileNotFoundError:
            pass
        raise


def _read(plan_id: str) -> dict[str, Any]:
    path = _plan_path(plan_id)
    try:
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size > _MAX_PLAN_BYTES
        ):
            raise GuangYaFSChangeError("光鸭变更计划不存在或已过期")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GuangYaFSChangeError("光鸭变更计划不存在或已过期") from exc
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise GuangYaFSChangeError("光鸭变更计划文件损坏") from exc
    if (
        not isinstance(payload, dict)
        or int(payload.get("version") or 0) != _PLAN_VERSION
    ):
        raise GuangYaFSChangeError("光鸭变更计划版本无效")
    actual = str(payload.get("auth") or "")
    if not actual or not hmac.compare_digest(actual, _auth(payload)):
        raise GuangYaFSChangeError("光鸭变更计划完整性校验失败")
    return payload


def _append_journal(plan_id: str, event: dict[str, Any]) -> None:
    path = _journal_path(plan_id)
    _ensure_private_directory(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {"at": _now_iso(), **event},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        protect_private_file(path)


def _owner_digest(owner: str) -> str:
    owner_key = str(owner or "").strip()
    if not owner_key:
        raise GuangYaFSChangeError("光鸭变更计划缺少会话身份")
    return organize_operation_owner_digest(owner_key)


def load_fs_change_plan(
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
        raise GuangYaFSChangeError("光鸭变更计划不属于当前会话")
    fingerprint = str(payload.get("fingerprint") or "")
    if expected_fingerprint and not hmac.compare_digest(
        fingerprint, str(expected_fingerprint or "")
    ):
        raise GuangYaFSChangeStale("光鸭变更计划已变化，请重新预览")
    now_epoch = time.time()
    if require_confirmed:
        if str(payload.get("status") or "") not in {"confirmed", "running"}:
            raise GuangYaFSChangeStale("光鸭变更计划已执行或不再可执行")
        if (
            float(payload.get("confirmed_at_epoch") or 0) <= 0
            or float(payload.get("execute_until_epoch") or 0) <= now_epoch
        ):
            raise GuangYaFSChangeStale("光鸭变更确认已过期，请重新预览")
    elif float(payload.get("expires_at_epoch") or 0) <= now_epoch:
        raise GuangYaFSChangeStale("光鸭变更预览已过期，请重新生成")
    return payload


def confirm_fs_change_plan(
    plan_id: str, *, owner: str, expected_fingerprint: str
) -> dict[str, Any]:
    payload = load_fs_change_plan(
        plan_id, owner=owner, expected_fingerprint=expected_fingerprint
    )
    if str(payload.get("status") or "") not in {"previewed", "confirmed"}:
        raise GuangYaFSChangeStale("光鸭变更计划已进入执行阶段，请重新预览")
    current = time.time()
    payload["confirmed_at"] = _now_iso()
    payload["confirmed_at_epoch"] = current
    payload["execute_until_epoch"] = current + _EXECUTE_TTL_SECONDS
    payload["status"] = "confirmed"
    _atomic_write(payload)
    return payload


def discard_fs_change_plan(plan_id: str) -> None:
    try:
        for path in (_plan_path(plan_id), _journal_path(plan_id)):
            if path.is_file() and not path.is_symlink():
                path.unlink()
    except (OSError, GuangYaFSChangeError):
        return


def maintain_fs_change_plans(*, preserve_plan_id: str = "") -> dict[str, int]:
    directory = _directory()
    if not directory.exists() or directory.is_symlink():
        return {"removed": 0, "remaining": 0}
    preserved = str(preserve_plan_id or "").strip().casefold()
    current = time.time()
    rows: list[tuple[float, str, str]] = []
    removed = 0
    for path in directory.glob("*.json"):
        try:
            payload = _read(path.stem)
            expiry = max(
                float(payload.get("expires_at_epoch") or 0),
                float(payload.get("execute_until_epoch") or 0),
            )
            if expiry <= current:
                discard_fs_change_plan(path.stem)
                removed += 1
                continue
            rows.append(
                (
                    float(payload.get("created_at_epoch") or path.stat().st_mtime),
                    path.stem,
                    str(payload.get("owner_digest") or ""),
                )
            )
        except (OSError, GuangYaFSChangeError, TypeError, ValueError):
            discard_fs_change_plan(path.stem)
            removed += 1
    rows.sort()

    def remove(row: tuple[float, str, str]) -> None:
        nonlocal removed
        discard_fs_change_plan(row[1])
        try:
            rows.remove(row)
        except ValueError:
            return
        removed += 1

    for owner_digest in {row[2] for row in rows if row[2]}:
        owner_rows = [row for row in rows if row[2] == owner_digest]
        while len(owner_rows) > _MAX_PLANS_PER_OWNER:
            victim = next((row for row in owner_rows if row[1] != preserved), None)
            if victim is None:
                break
            remove(victim)
            owner_rows.remove(victim)
    while len(rows) > _MAX_PLANS:
        victim = next((row for row in rows if row[1] != preserved), None)
        if victim is None:
            break
        remove(victim)
    return {"removed": removed, "remaining": len(rows)}


def _normalize_path(value: object, *, allow_root: bool = True) -> str:
    path = str(value or "").strip().replace("\\", "/")
    if not path.startswith("/") or len(path) > 2048:
        raise GuangYaFSChangeError("光鸭目录路径必须是绝对路径")
    parts = [part for part in path.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise GuangYaFSChangeError("光鸭目录路径不能包含相对组件")
    if not parts:
        if allow_root:
            return "/"
        raise GuangYaFSChangeError("该操作不能以光鸭根目录为对象")
    return "/" + "/".join(parts)


def _validate_name(value: object) -> str:
    if not isinstance(value, str):
        raise GuangYaFSChangeError("目标名称格式无效")
    name = value.strip()
    if (
        not name
        or name in {".", ".."}
        or len(name) > 255
        or "/" in name
        or "\\" in name
        or _CONTROL.search(name)
    ):
        raise GuangYaFSChangeError("目标名称包含不安全字符或长度无效")
    return name


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


def _snapshot_matches(item: GuangYaFile | None, snapshot: dict[str, Any]) -> bool:
    if item is None:
        return False
    if not (
        str(item.file_id) == str(snapshot.get("file_id") or "")
        and str(item.parent_id) == str(snapshot.get("parent_id") or "0")
        and str(item.name) == str(snapshot.get("name") or "")
        and bool(item.is_dir) == bool(snapshot.get("is_dir"))
        and max(0, int(item.size or 0)) == max(0, int(snapshot.get("size") or 0))
        and str(item.etag or "") == str(snapshot.get("etag") or "")
    ):
        return False
    expected_updated = max(0, int(snapshot.get("updated_at") or 0))
    return (
        bool(item.etag)
        or not expected_updated
        or max(0, int(item.updated_at or 0)) == expected_updated
    )


def _full_path(parent_path: str, name: str) -> str:
    return (parent_path.rstrip("/") + "/" + name) if parent_path != "/" else "/" + name


def _resolve_directory(
    client: GuangYaClient, path: str
) -> tuple[str, dict[str, Any] | None]:
    normalized = _normalize_path(path)
    if normalized == "/":
        return "0", None
    target = resolve_workspace_path(client, normalized)
    if not target.is_dir:
        raise GuangYaFSChangeError("目标路径不是光鸭目录")
    return str(target.file_id), _snapshot(target)


def _list_map(
    client: GuangYaClient,
    parent_id: str,
    cache: dict[str, dict[str, GuangYaFile]],
) -> dict[str, GuangYaFile]:
    if parent_id not in cache:
        cache[parent_id] = {
            str(item.file_id): item for item in client.list_dir(parent_id)
        }
    return cache[parent_id]


def _name_conflict(
    items: dict[str, GuangYaFile], name: str, *, exclude_id: str = ""
) -> bool:
    folded = name.casefold()
    return any(
        item.name.casefold() == folded and str(item.file_id) != str(exclude_id or "")
        for item in items.values()
    )


def _validate_observation(
    client: GuangYaClient, owner: str, observation: dict[str, Any]
) -> None:
    if not isinstance(observation, dict):
        raise GuangYaFSChangeError("光鸭观察快照格式无效")
    if not hmac.compare_digest(
        str(observation.get("owner_digest") or ""), _owner_digest(owner)
    ):
        raise GuangYaFSChangeError("光鸭观察快照不属于当前会话")
    if float(observation.get("expires_at_epoch") or 0) <= time.time():
        raise GuangYaWorkspaceStale("光鸭目录观察已过期，请重新读取")
    if int(observation.get("credential_generation") or -1) != int(
        client.credential_generation
    ):
        raise GuangYaWorkspaceStale("光鸭登录凭据已变化，请重新读取目录")


def build_fs_change_plan(
    client: GuangYaClient,
    *,
    owner: str,
    observation: dict[str, Any],
    operations: list[dict[str, Any]],
    trigger_strm: bool = True,
) -> dict[str, Any]:
    _validate_observation(client, owner, observation)
    if not isinstance(operations, list) or not 1 <= len(operations) <= _MAX_OPERATIONS:
        raise GuangYaFSChangeError(
            f"光鸭变更计划必须包含 1 到 {_MAX_OPERATIONS} 项操作"
        )
    entries = observation_entry_map(observation)
    cache: dict[str, dict[str, GuangYaFile]] = {}
    frozen: list[dict[str, Any]] = []
    seen_objects: set[str] = set()
    seen_creates: set[tuple[str, str]] = set()

    for raw in operations:
        if not isinstance(raw, dict):
            raise GuangYaFSChangeError("光鸭变更操作格式无效")
        op = str(raw.get("op") or "").strip().casefold()
        if op not in {"rename", "move", "trash", "create_directory"}:
            raise GuangYaFSChangeError("不支持的光鸭变更操作")
        if op == "create_directory":
            parent_path = _normalize_path(raw.get("parent_path"))
            name = _validate_name(raw.get("name"))
            parent_id, parent_snapshot = _resolve_directory(client, parent_path)
            key = (parent_id, name.casefold())
            if key in seen_creates:
                raise GuangYaFSChangeError("计划不能重复新建同名目录")
            seen_creates.add(key)
            siblings = _list_map(client, parent_id, cache)
            if _name_conflict(siblings, name):
                raise GuangYaFSChangeError("新建目录名称已被占用")
            frozen.append(
                {
                    "op": op,
                    "parent_path": parent_path,
                    "parent_id": parent_id,
                    "parent_snapshot": parent_snapshot,
                    "name": name,
                }
            )
            continue

        handle = str(raw.get("object_ref") or "").strip().upper()
        if not valid_object_handle(handle) or handle not in entries:
            raise GuangYaFSChangeError("计划包含当前观察中不存在的对象引用")
        if handle in seen_objects:
            raise GuangYaFSChangeError("计划不能重复操作同一个对象")
        seen_objects.add(handle)
        observed = entries[handle]
        parent_id = str(observed.get("parent_id") or "0")
        current = _list_map(client, parent_id, cache).get(
            str(observed.get("file_id") or "")
        )
        if not _snapshot_matches(current, observed):
            raise GuangYaWorkspaceStale("光鸭对象已变化，请重新读取后再生成计划")
        if current is None:
            raise GuangYaWorkspaceStale("光鸭对象已变化，请重新读取后再生成计划")
        base = {
            "op": op,
            "object_ref": handle,
            "source": _snapshot(current),
            "source_parent_path": str(observed.get("parent_path") or "/"),
            "source_path": _full_path(
                str(observed.get("parent_path") or "/"), str(current.name)
            ),
        }
        if op == "rename":
            new_name = _validate_name(raw.get("new_name"))
            if new_name == current.name:
                raise GuangYaFSChangeError("改名操作没有产生变化")
            if not current.is_dir:
                old_suffix = Path(current.name).suffix.casefold()
                new_suffix = Path(new_name).suffix.casefold()
                if old_suffix != new_suffix:
                    raise GuangYaFSChangeError("文件改名不能改变扩展名")
            if _name_conflict(cache[parent_id], new_name, exclude_id=current.file_id):
                raise GuangYaFSChangeError("改名目标已被同目录对象占用")
            base["new_name"] = new_name
        elif op == "move":
            target_path = _normalize_path(raw.get("target_path"))
            target_id, target_snapshot = _resolve_directory(client, target_path)
            if target_id == parent_id:
                raise GuangYaFSChangeError("移动目标与当前目录相同")
            source_path = str(base["source_path"])
            if current.is_dir and (
                target_path == source_path or target_path.startswith(source_path + "/")
            ):
                raise GuangYaFSChangeError("不能把目录移动到自身或其子目录")
            target_items = _list_map(client, target_id, cache)
            if _name_conflict(target_items, current.name):
                raise GuangYaFSChangeError("移动目标中已有同名对象")
            base.update(
                target_path=target_path,
                target_id=target_id,
                target_snapshot=target_snapshot,
            )
        frozen.append(base)

    structural_paths = [
        str(item.get("source_path") or "")
        for item in frozen
        if item.get("op") in {"move", "trash"}
        and bool((item.get("source") or {}).get("is_dir"))
    ]
    for item in frozen:
        source_path = str(item.get("source_path") or "")
        if not source_path:
            continue
        for parent_path in structural_paths:
            if source_path != parent_path and source_path.startswith(parent_path + "/"):
                raise GuangYaFSChangeError(
                    "同一计划不能同时移动或回收父目录并操作其内部对象"
                )

    counts = {key: 0 for key in ("rename", "move", "trash", "create_directory")}
    samples: list[str] = []
    for item in frozen:
        op = str(item["op"])
        counts[op] += 1
        if len(samples) >= 6:
            continue
        if op == "rename":
            samples.append(f"改名：{item['source']['name']} → {item['new_name']}")
        elif op == "move":
            samples.append(
                f"移动：{item['source']['name']} → {Path(str(item['target_path'])).name or '根目录'}"
            )
        elif op == "trash":
            samples.append(f"移入回收站：{item['source']['name']}")
        else:
            samples.append(f"新建目录：{item['name']}")

    current = time.time()
    plan = {
        "version": _PLAN_VERSION,
        "plan_id": uuid.uuid4().hex,
        "owner_digest": _owner_digest(owner),
        "credential_generation": int(client.credential_generation),
        "observation_ref": observation_ref(str(observation.get("plan_id") or "")),
        "created_at": _now_iso(),
        "created_at_epoch": current,
        "expires_at_epoch": current + _PLAN_TTL_SECONDS,
        "trigger_strm": bool(trigger_strm),
        "status": "previewed",
        "operations": frozen,
        "stats": {"total": len(frozen), **counts},
        "samples": samples,
        "execution": {},
    }
    plan["fingerprint"] = _fingerprint(plan)
    _atomic_write(plan)
    maintain_fs_change_plans(preserve_plan_id=str(plan["plan_id"]))
    if not _plan_path(str(plan["plan_id"])).exists():
        raise GuangYaFSChangeError("光鸭变更计划容量已满，请稍后重试")
    return plan


def _find_current(
    client: GuangYaClient, snapshot: dict[str, Any]
) -> GuangYaFile | None:
    try:
        current = client.file_info(str(snapshot.get("file_id") or ""))
    except Exception:  # noqa: BLE001 - Provider/SDK 可抛出多种传输异常
        current = None
    if _snapshot_matches(current, snapshot):
        return current
    try:
        return {
            str(item.file_id): item
            for item in client.list_dir(str(snapshot.get("parent_id") or "0"))
        }.get(str(snapshot.get("file_id") or ""))
    except Exception:  # noqa: BLE001 - 只读回退失败按对象不存在处理
        return None


def _verify_directory_snapshot(
    client: GuangYaClient, directory_id: str, snapshot: dict[str, Any] | None
) -> bool:
    if directory_id == "0":
        return True
    if not isinstance(snapshot, dict):
        return False
    current = _find_current(client, snapshot)
    # 目录内容变化可能更新 etag/utime；目标目录只冻结身份、名称与位置，
    # 同名占用会在每一项写入前另行完整读取。
    return bool(
        current
        and current.is_dir
        and str(current.file_id) == str(snapshot.get("file_id") or "")
        and str(current.parent_id) == str(snapshot.get("parent_id") or "0")
        and str(current.name) == str(snapshot.get("name") or "")
    )


def _preflight_operation(client: GuangYaClient, item: dict[str, Any]) -> None:
    op = str(item.get("op") or "")
    if op == "create_directory":
        parent_id = str(item.get("parent_id") or "0")
        if not _verify_directory_snapshot(
            client, parent_id, item.get("parent_snapshot")
        ):
            raise GuangYaFSChangeStale("新建目录的父目录已变化，请重新预览")
        siblings = {str(row.file_id): row for row in client.list_dir(parent_id)}
        if _name_conflict(siblings, str(item.get("name") or "")):
            raise GuangYaFSChangeStale("新建目录名称已被占用，请重新预览")
        return
    source = item.get("source")
    if not isinstance(source, dict) or not _snapshot_matches(
        _find_current(client, source), source
    ):
        raise GuangYaFSChangeStale("光鸭对象已变化，请重新预览")
    if op == "rename":
        siblings = {
            str(row.file_id): row
            for row in client.list_dir(str(source.get("parent_id") or "0"))
        }
        if _name_conflict(
            siblings,
            str(item.get("new_name") or ""),
            exclude_id=str(source.get("file_id") or ""),
        ):
            raise GuangYaFSChangeStale("改名目标已被占用，请重新预览")
    elif op == "move":
        target_id = str(item.get("target_id") or "0")
        if not _verify_directory_snapshot(
            client, target_id, item.get("target_snapshot")
        ):
            raise GuangYaFSChangeStale("移动目标目录已变化，请重新预览")
        siblings = {str(row.file_id): row for row in client.list_dir(target_id)}
        if _name_conflict(siblings, str(source.get("name") or "")):
            raise GuangYaFSChangeStale("移动目标中已有同名对象，请重新预览")


def _verify_after(
    client: GuangYaClient, item: dict[str, Any], created_id: str = ""
) -> bool:
    op = str(item.get("op") or "")
    if op == "create_directory":
        parent_id = str(item.get("parent_id") or "0")
        matches = [
            row
            for row in client.list_dir(parent_id)
            if row.is_dir and row.name == str(item.get("name") or "")
        ]
        return len(matches) == 1 and (
            not created_id or str(matches[0].file_id) == str(created_id)
        )
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    file_id = str(source.get("file_id") or "")
    if op == "trash":
        return all(
            str(row.file_id) != file_id
            for row in client.list_dir(str(source.get("parent_id") or "0"))
        )
    if op == "rename":
        return any(
            str(row.file_id) == file_id and row.name == str(item.get("new_name") or "")
            for row in client.list_dir(str(source.get("parent_id") or "0"))
        )
    if op == "move":
        target_id = str(item.get("target_id") or "0")
        return any(
            str(row.file_id) == file_id and row.name == str(source.get("name") or "")
            for row in client.list_dir(target_id)
        )
    return False


def update_fs_change_plan_execution(
    plan_id: str, *, status: str, execution: dict[str, Any]
) -> None:
    payload = _read(plan_id)
    payload["status"] = str(status)
    payload["execution"] = dict(execution)
    payload["updated_at"] = _now_iso()
    _atomic_write(payload)


def execute_fs_change_plan(
    payload: dict[str, Any],
    *,
    cancel_check: Callable[[], None] | None = None,
    client_factory: Callable[[], GuangYaClient] = GuangYaClient,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or int(payload.get("version") or 0) != 1:
        raise GuangYaFSChangeError("光鸭变更任务参数无效")
    plan_id = str(payload.get("plan_id") or "")
    expected_fingerprint = str(payload.get("plan_fingerprint") or "")
    plan = load_fs_change_plan(
        plan_id,
        expected_fingerprint=expected_fingerprint,
        require_confirmed=True,
    )
    if not hmac.compare_digest(
        str(plan.get("owner_digest") or ""), str(payload.get("owner_digest") or "")
    ):
        raise GuangYaFSChangeError("光鸭变更任务会话不匹配")
    expected_generation = int(payload.get("credential_generation") or -1)
    client = client_factory()
    stats = {
        "total": len(plan.get("operations") or []),
        "renamed": 0,
        "moved": 0,
        "trashed": 0,
        "created": 0,
        "failed": 0,
        "verification_failed": 0,
        "precondition_failed": 0,
    }
    started_at = _now_iso()
    try:
        if (
            not client.logged_in
            or int(client.credential_generation) != expected_generation
        ):
            raise GuangYaFSChangeStale("光鸭登录凭据已变化，请重新预览")
        operations = list(plan.get("operations") or [])
        if not operations:
            raise GuangYaFSChangeError("光鸭变更计划没有可执行对象")
        for item in operations:
            if cancel_check is not None:
                cancel_check()
            _preflight_operation(client, item)
        update_fs_change_plan_execution(
            plan_id,
            status="running",
            execution={"started_at": started_at, **stats},
        )
        _append_journal(
            plan_id,
            {"action": "preflight", "status": "completed", "total": len(operations)},
        )
        for index, item in enumerate(operations, start=1):
            if cancel_check is not None:
                cancel_check()
            op = str(item.get("op") or "")
            source = item.get("source") if isinstance(item.get("source"), dict) else {}
            created_id = ""
            error_type = ""
            provider_code = ""
            try:
                _preflight_operation(client, item)
                if op == "rename":
                    client.rename(
                        str(source.get("file_id") or ""), str(item["new_name"])
                    )
                elif op == "move":
                    client.move(
                        [str(source.get("file_id") or "")],
                        str(item.get("target_id") or "0"),
                    )
                elif op == "trash":
                    client.delete([str(source.get("file_id") or "")])
                elif op == "create_directory":
                    created_id = client.create_dir(
                        str(item["name"]), str(item.get("parent_id") or "0")
                    )
                else:
                    raise GuangYaFSChangeError("光鸭变更计划包含未知操作")
                if not _verify_after(client, item, created_id):
                    stats["verification_failed"] += 1
                    raise GuangYaFSChangeError("写入后的云端状态校验失败")
                stat_key = {
                    "rename": "renamed",
                    "move": "moved",
                    "trash": "trashed",
                    "create_directory": "created",
                }[op]
                stats[stat_key] += 1
                status = "completed"
            except GuangYaFSChangeStale as exc:
                error_type = type(exc).__name__
                stats["precondition_failed"] += 1
                stats["failed"] += 1
                status = "failed"
            except GuangYaWriteRejected as exc:
                error_type = type(exc).__name__
                provider_code = str(exc.code or "")
                stats["failed"] += 1
                status = "failed"
            except Exception as exc:  # noqa: BLE001 - 单项失败需记录后继续其余冻结操作
                error_type = type(exc).__name__
                stats["failed"] += 1
                status = "failed"
            _append_journal(
                plan_id,
                {
                    "action": op,
                    "index": index,
                    "file_id": str(source.get("file_id") or created_id),
                    "status": status,
                    "error_type": error_type,
                    "provider_code": provider_code,
                },
            )
        successful = (
            stats["renamed"] + stats["moved"] + stats["trashed"] + stats["created"]
        )
        partial = stats["failed"] > 0
        if bool(plan.get("trigger_strm")) and successful > 0:
            try:
                from app.modules.scheduler import get_scheduler

                triggered = get_scheduler().trigger(
                    "organize", force_full=True, sync_mode="full"
                )
            except Exception:  # noqa: BLE001 - STRM 联动失败不回滚已完成云端写入
                triggered = {"ok": False}
            if bool(triggered.get("ok")):
                stats["strm_triggered"] = 1
            else:
                stats["strm_trigger_failed"] = 1
                partial = True
        finished_at = _now_iso()
        final_status = "partial" if partial else "completed"
        update_fs_change_plan_execution(
            plan_id,
            status=final_status,
            execution={"started_at": started_at, "finished_at": finished_at, **stats},
        )
        return {"partial": partial, "stats": stats}
    finally:
        client.close()
