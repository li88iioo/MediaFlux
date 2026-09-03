"""光鸭文件系统通用变更的私有冻结计划与可验证执行器。"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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
from app.modules.organize_delete_audit import (
    DeleteCandidate,
    execute_recycle_bin_delete,
)
from app.modules.process_lock import CrossProcessLock
from app.modules.web_secret import get_web_secret
from app.private_files import protect_private_file
from app.repositories.organize_operation_jobs import organize_operation_owner_digest

_PLAN_VERSION = 1
_PLAN_TTL_SECONDS = 10 * 60
_EXECUTE_TTL_SECONDS = 15 * 60
_MAX_OPERATIONS = 200
_MAX_PLAN_BYTES = 2 * 1024 * 1024
_MAX_PLANS = 32
_MAX_PLANS_PER_OWNER = 4
_SAFE_PLAN_ID = re.compile(r"^[0-9a-f]{32}$")
_SAFE_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_ACTIVE_PLAN_STATUSES = frozenset({"confirmed", "queued", "running"})
_TERMINAL_PLAN_STATUSES = frozenset(
    {"completed", "partial", "failed", "cancelled", "manual_review"}
)

logger = logging.getLogger(__name__)


class GuangYaFSChangeError(RuntimeError):
    """可安全映射给 Agent 的通用光鸭变更错误。"""


class GuangYaFSChangeStale(GuangYaFSChangeError):
    """冻结计划已过期或云端对象已变化。"""


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _directory() -> Path:
    return Path(PATHS.data_dir) / "agent-guangya-fs-change"


@contextmanager
def _plan_state_lock() -> Iterator[None]:
    """串行化计划文件的状态转换与清理。

    计划文件本身通过 ``os.replace`` 保证单次写入原子，但读取后再写回仍需
    跨进程互斥，否则确认、队列绑定、执行领取与 GC 会发生 lost update。
    锁放在计划目录内，使测试替换目录时也不会触碰真实数据库目录。
    """
    lock = CrossProcessLock("guangya-fs-change-state", directory=_directory())
    if not lock.acquire():  # blocking=True 理论上只会返回 True
        raise GuangYaFSChangeError("光鸭变更计划当前正被其他进程更新")
    try:
        yield
    finally:
        lock.release()


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


def _validate_plan_identity(
    payload: dict[str, Any],
    *,
    owner: str | None = None,
    owner_digest: str = "",
    expected_fingerprint: str = "",
) -> None:
    expected_owner = (
        _owner_digest(owner) if owner is not None else str(owner_digest or "")
    )
    if expected_owner and not hmac.compare_digest(
        str(payload.get("owner_digest") or ""), expected_owner
    ):
        raise GuangYaFSChangeError("光鸭变更计划不属于当前会话")
    if expected_fingerprint and not hmac.compare_digest(
        str(payload.get("fingerprint") or ""), str(expected_fingerprint or "")
    ):
        raise GuangYaFSChangeStale("光鸭变更计划已变化，请重新预览")


def _validate_plan_lifetime(
    payload: dict[str, Any], *, require_confirmed: bool = False
) -> None:
    status = str(payload.get("status") or "")
    current = time.time()
    if require_confirmed:
        if status not in {"confirmed", "queued", "running"}:
            raise GuangYaFSChangeStale("光鸭变更计划已执行或不再可执行")
        # 一旦持久任务已绑定，队列自身的 expires_at/lease 才是执行准入依据；
        # 不能让用户已经确认并成功入队的任务再次受 15 分钟票据限制。
        if status == "confirmed" and (
            float(payload.get("confirmed_at_epoch") or 0) <= 0
            or float(payload.get("execute_until_epoch") or 0) <= current
        ):
            raise GuangYaFSChangeStale("光鸭变更确认已过期，请重新预览")
        return
    if status == "previewed":
        if float(payload.get("expires_at_epoch") or 0) <= current:
            raise GuangYaFSChangeStale("光鸭变更预览已过期，请重新生成")
    elif (
        status == "confirmed"
        and float(payload.get("execute_until_epoch") or 0) <= current
    ):
        raise GuangYaFSChangeStale("光鸭变更确认已过期，请重新预览")


def load_fs_change_plan(
    plan_id: str,
    *,
    owner: str | None = None,
    expected_fingerprint: str = "",
    require_confirmed: bool = False,
) -> dict[str, Any]:
    payload = _read(plan_id)
    _validate_plan_identity(
        payload, owner=owner, expected_fingerprint=expected_fingerprint
    )
    _validate_plan_lifetime(payload, require_confirmed=require_confirmed)
    return payload


def confirm_fs_change_plan(
    plan_id: str, *, owner: str, expected_fingerprint: str
) -> dict[str, Any]:
    with _plan_state_lock():
        payload = _read(plan_id)
        _validate_plan_identity(
            payload, owner=owner, expected_fingerprint=expected_fingerprint
        )
        _validate_plan_lifetime(payload)
        if str(payload.get("status") or "") not in {"previewed", "confirmed"}:
            raise GuangYaFSChangeStale("光鸭变更计划已进入执行阶段，请重新预览")
        current = time.time()
        payload["confirmed_at"] = _now_iso()
        payload["confirmed_at_epoch"] = current
        payload["execute_until_epoch"] = current + _EXECUTE_TTL_SECONDS
        payload["status"] = "confirmed"
        payload["updated_at"] = _now_iso()
        _atomic_write(payload)
        return payload


def bind_fs_change_plan_job(
    plan_id: str,
    *,
    owner_digest: str,
    expected_fingerprint: str,
    job_id: str,
    queue_until_epoch: float,
) -> dict[str, Any]:
    """把已确认计划原子绑定到唯一持久任务。

    此函数由队列仓储在 SQLite 写事务提交前调用。其他进程在事务提交前看不
    到任务，因此不会出现任务先领取、计划仍停留在 ``confirmed`` 的窗口。
    """
    safe_job_id = str(job_id or "").strip().casefold()
    if not _SAFE_JOB_ID.fullmatch(safe_job_id):
        raise GuangYaFSChangeError("光鸭变更任务编号无效")
    safe_queue_until = float(queue_until_epoch or 0)
    if safe_queue_until <= time.time():
        raise GuangYaFSChangeStale("光鸭变更任务已过期，请重新预览")
    with _plan_state_lock():
        payload = _read(plan_id)
        _validate_plan_identity(
            payload,
            owner_digest=owner_digest,
            expected_fingerprint=expected_fingerprint,
        )
        status = str(payload.get("status") or "")
        bound_job_id = str(payload.get("job_id") or "")
        if status == "confirmed":
            _validate_plan_lifetime(payload, require_confirmed=True)
        elif status in {"queued", "running"}:
            if not bound_job_id or not hmac.compare_digest(bound_job_id, safe_job_id):
                raise GuangYaFSChangeStale("光鸭变更计划已绑定其他任务")
        else:
            raise GuangYaFSChangeStale("光鸭变更计划已执行或不再可排队")
        payload["status"] = "running" if status == "running" else "queued"
        payload["job_id"] = safe_job_id
        payload["queued_at"] = str(payload.get("queued_at") or _now_iso())
        payload["queue_until_epoch"] = max(
            safe_queue_until, float(payload.get("queue_until_epoch") or 0)
        )
        payload["updated_at"] = _now_iso()
        _atomic_write(payload)
        return payload


def finalize_fs_change_plan_job(
    plan_id: str,
    *,
    expected_fingerprint: str,
    job_id: str,
    queue_status: str,
    audit_failures: int = 0,
    error_code: str = "",
) -> dict[str, Any]:
    """把持久队列终态投影回冻结计划，避免遗留永久 queued/running。"""
    safe_job_id = str(job_id or "").strip().casefold()
    safe_queue_status = str(queue_status or "").strip().casefold()
    if not _SAFE_JOB_ID.fullmatch(safe_job_id):
        raise GuangYaFSChangeError("光鸭变更任务编号无效")
    if safe_queue_status not in _TERMINAL_PLAN_STATUSES:
        raise GuangYaFSChangeError("光鸭变更任务终态无效")
    with _plan_state_lock():
        payload = _read(plan_id)
        _validate_plan_identity(payload, expected_fingerprint=expected_fingerprint)
        current_status = str(payload.get("status") or "")
        bound_job_id = str(payload.get("job_id") or "")
        if bound_job_id and not hmac.compare_digest(bound_job_id, safe_job_id):
            raise GuangYaFSChangeStale("光鸭变更计划任务绑定已变化")
        if current_status in _TERMINAL_PLAN_STATUSES:
            return payload
        if current_status not in {"queued", "running"}:
            raise GuangYaFSChangeStale("光鸭变更计划状态已变化")
        # running 说明 provider 写入窗口已经打开；若执行器没能先写入自己的
        # 终态，队列无论收到 failed/partial/completed 都只能标人工核验。
        if (
            current_status == "running"
            or safe_queue_status in {"completed", "partial", "manual_review"}
            or max(0, int(audit_failures or 0)) > 0
        ):
            plan_status = "manual_review"
        else:
            plan_status = safe_queue_status
        execution = (
            dict(payload.get("execution"))
            if isinstance(payload.get("execution"), dict)
            else {}
        )
        execution.update(
            {
                "queue_status": safe_queue_status,
                "queue_error_code": str(error_code or "")[:80],
                "finished_at": str(execution.get("finished_at") or _now_iso()),
            }
        )
        payload["status"] = plan_status
        payload["job_id"] = safe_job_id
        payload["execution"] = execution
        payload["updated_at"] = _now_iso()
        _atomic_write(payload)
        return payload


def _discard_fs_change_plan_unlocked(plan_id: str) -> bool:
    removed = False
    for path in (_plan_path(plan_id), _journal_path(plan_id)):
        try:
            if path.is_file() and not path.is_symlink():
                path.unlink()
                removed = True
        except FileNotFoundError:
            continue
    return removed


def discard_fs_change_plan(plan_id: str, *, preview_only: bool = False) -> bool:
    """删除计划；预览替换路径只能清理尚未确认的旧预览。"""
    try:
        with _plan_state_lock():
            if preview_only:
                try:
                    payload = _read(plan_id)
                except GuangYaFSChangeError:
                    return False
                if str(payload.get("status") or "") != "previewed":
                    return False
            return _discard_fs_change_plan_unlocked(plan_id)
    except (OSError, GuangYaFSChangeError):
        return False


def _plan_is_expired(payload: dict[str, Any], current_epoch: float) -> bool:
    status = str(payload.get("status") or "")
    # running 是已经打开远端写窗口的执行凭据，必须等待队列恢复显式收束。
    # queued 尚未产生 Provider 副作用；其持久队列 TTL 到期后任务本身已不可领取，
    # 因而可安全清理，也能回收“计划已绑定、SQLite 提交前硬崩溃”的孤立文件。
    # 此处只读计划自身的 queue_until_epoch，不访问数据库，继续保持固定锁序。
    if status == "running":
        return False
    if status == "queued":
        queue_until = float(
            payload.get("queue_until_epoch") or payload.get("execute_until_epoch") or 0
        )
        return queue_until > 0 and queue_until <= current_epoch
    if status == "confirmed":
        return float(payload.get("execute_until_epoch") or 0) <= current_epoch
    return (
        max(
            float(payload.get("expires_at_epoch") or 0),
            float(payload.get("execute_until_epoch") or 0),
        )
        <= current_epoch
    )


def _maintain_fs_change_plans_unlocked(*, preserve_plan_id: str = "") -> dict[str, int]:
    directory = _directory()
    if not directory.exists() or directory.is_symlink():
        return {
            "removed": 0,
            "remaining": 0,
            "owner_remaining": 0,
            "capacity_exceeded": 0,
        }
    preserved = str(preserve_plan_id or "").strip().casefold()
    current = time.time()
    rows: list[dict[str, Any]] = []
    removed = 0
    for path in directory.glob("*.json"):
        try:
            payload = _read(path.stem)
            if _plan_is_expired(payload, current):
                _discard_fs_change_plan_unlocked(path.stem)
                removed += 1
                continue
            status = str(payload.get("status") or "")
            rows.append(
                {
                    "created": float(
                        payload.get("created_at_epoch") or path.stat().st_mtime
                    ),
                    "plan_id": path.stem,
                    "owner_digest": str(payload.get("owner_digest") or ""),
                    "status": status,
                    "removable": status not in _ACTIVE_PLAN_STATUSES,
                }
            )
        except (OSError, GuangYaFSChangeError, TypeError, ValueError):
            try:
                _discard_fs_change_plan_unlocked(path.stem)
            except (OSError, GuangYaFSChangeError):
                pass
            removed += 1
    rows.sort(key=lambda row: (float(row["created"]), str(row["plan_id"])))

    def remove(row: dict[str, Any]) -> None:
        nonlocal removed
        _discard_fs_change_plan_unlocked(str(row["plan_id"]))
        try:
            rows.remove(row)
        except ValueError:
            return
        removed += 1

    for digest in {str(row["owner_digest"]) for row in rows if row["owner_digest"]}:
        owner_rows = [row for row in rows if row["owner_digest"] == digest]
        while len(owner_rows) > _MAX_PLANS_PER_OWNER:
            victim = next(
                (
                    row
                    for row in owner_rows
                    if row["plan_id"] != preserved and bool(row["removable"])
                ),
                None,
            )
            if victim is None:
                break
            remove(victim)
            owner_rows.remove(victim)
    while len(rows) > _MAX_PLANS:
        victim = next(
            (
                row
                for row in rows
                if row["plan_id"] != preserved and bool(row["removable"])
            ),
            None,
        )
        if victim is None:
            break
        remove(victim)
    preserved_row = next((row for row in rows if row["plan_id"] == preserved), None)
    preserved_owner = (
        str(preserved_row["owner_digest"]) if preserved_row is not None else ""
    )
    owner_remaining = sum(
        1 for row in rows if preserved_owner and row["owner_digest"] == preserved_owner
    )
    capacity_exceeded = int(
        len(rows) > _MAX_PLANS
        or bool(preserved_owner and owner_remaining > _MAX_PLANS_PER_OWNER)
    )
    return {
        "removed": removed,
        "remaining": len(rows),
        "owner_remaining": owner_remaining,
        "capacity_exceeded": capacity_exceeded,
    }


def maintain_fs_change_plans(*, preserve_plan_id: str = "") -> dict[str, int]:
    with _plan_state_lock():
        return _maintain_fs_change_plans_unlocked(preserve_plan_id=preserve_plan_id)


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
    raw_generation = observation.get("credential_generation")
    expected_generation = int(raw_generation) if raw_generation is not None else -1
    if expected_generation != int(client.credential_generation):
        raise GuangYaWorkspaceStale("光鸭登录凭据已变化，请重新读取目录")


def _expand_batch_operations(
    operations: list[dict[str, Any]], entries: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for raw in operations:
        if str(raw.get("op") or "").strip().casefold() != "batch_relocate":
            expanded.append(raw)
            continue
        title = str(raw.get("title") or "").strip()
        naming = str(raw.get("naming") or "absolute").strip().casefold()
        season = int(raw.get("season", 1))
        padding = int(raw.get("episode_padding", 2))
        target_path = _normalize_path(raw.get("target_path"))
        if naming not in {"season_episode", "absolute"}:
            raise GuangYaFSChangeError("批量规整命名方式无效")
        if not 0 <= season <= 999 or not 2 <= padding <= 4:
            raise GuangYaFSChangeError("批量规整编号参数无效")
        items = raw.get("items")
        if not isinstance(items, list) or not items:
            raise GuangYaFSChangeError("批量规整缺少对象与集号")
        for item in items:
            if not isinstance(item, dict):
                raise GuangYaFSChangeError("批量规整条目格式无效")
            handle = str(item.get("object_ref") or "").strip().upper()
            if not valid_object_handle(handle) or handle not in entries:
                raise GuangYaFSChangeError("批量规整包含当前观察外的对象")
            episode = item.get("episode")
            if type(episode) is not int or not 1 <= episode <= 9999:
                raise GuangYaFSChangeError("批量规整集号无效")
            observed = entries[handle]
            if bool(observed.get("is_dir")):
                raise GuangYaFSChangeError("批量规整只支持文件")
            source_name = str(observed.get("name") or "")
            suffix = Path(source_name).suffix
            if not suffix:
                extension = str(observed.get("extension") or "").strip().lstrip(".")
                suffix = f".{extension}" if extension else ""
            number = str(episode).zfill(padding)
            marker = (
                f"S{season:02d}E{number}"
                if naming == "season_episode"
                else f"E{number}"
            )
            expanded.append(
                {
                    "op": "relocate",
                    "object_ref": handle,
                    "target_path": target_path,
                    "new_name": _validate_name(f"{title} - {marker}{suffix}"),
                }
            )
    return expanded


def build_fs_change_plan(
    client: GuangYaClient,
    *,
    owner: str,
    observation: dict[str, Any],
    operations: list[dict[str, Any]],
    trigger_strm: bool = True,
) -> dict[str, Any]:
    _validate_observation(client, owner, observation)
    if not isinstance(operations, list) or not operations:
        raise GuangYaFSChangeError(
            f"光鸭变更计划必须包含 1 到 {_MAX_OPERATIONS} 项操作"
        )
    entries = observation_entry_map(observation)
    operations = _expand_batch_operations(operations, entries)
    if not 1 <= len(operations) <= _MAX_OPERATIONS:
        raise GuangYaFSChangeError(
            f"展开后的光鸭变更计划必须包含 1 到 {_MAX_OPERATIONS} 项操作"
        )
    operations = [
        *(
            item
            for item in operations
            if str(item.get("op") or "").strip().casefold() == "create_directory"
        ),
        *(
            item
            for item in operations
            if str(item.get("op") or "").strip().casefold() != "create_directory"
        ),
    ]
    cache: dict[str, dict[str, GuangYaFile]] = {}
    frozen: list[dict[str, Any]] = []
    seen_objects: set[str] = set()
    seen_creates: set[tuple[str, str]] = set()
    pending_targets: dict[str, dict[str, Any]] = {}
    planned_target_names: dict[str, set[str]] = {}

    for raw in operations:
        if not isinstance(raw, dict):
            raise GuangYaFSChangeError("光鸭变更操作格式无效")
        op = str(raw.get("op") or "").strip().casefold()
        if op not in {"rename", "move", "relocate", "trash", "create_directory"}:
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
            created_path = _full_path(parent_path, name)
            if created_path in pending_targets:
                raise GuangYaFSChangeError("计划不能重复新建同一路径")
            created = {
                "op": op,
                "parent_path": parent_path,
                "parent_id": parent_id,
                "parent_snapshot": parent_snapshot,
                "name": name,
                "created_path": created_path,
            }
            pending_targets[created_path] = created
            frozen.append(created)
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
        if op in {"rename", "relocate"}:
            new_name = _validate_name(raw.get("new_name"))
            if op == "rename" and new_name == current.name:
                raise GuangYaFSChangeError("改名操作没有产生变化")
            if not current.is_dir:
                old_suffix = Path(current.name).suffix.casefold()
                new_suffix = Path(new_name).suffix.casefold()
                if old_suffix != new_suffix:
                    raise GuangYaFSChangeError("文件改名不能改变扩展名")
            if _name_conflict(cache[parent_id], new_name, exclude_id=current.file_id):
                raise GuangYaFSChangeError("改名目标已被同目录对象占用")
            base["new_name"] = new_name
        if op in {"move", "relocate"}:
            target_path = _normalize_path(raw.get("target_path"))
            pending_target = pending_targets.get(target_path)
            if pending_target is None:
                target_id, target_snapshot = _resolve_directory(client, target_path)
                if target_id == parent_id:
                    raise GuangYaFSChangeError("移动目标与当前目录相同")
            else:
                target_id, target_snapshot = "", None
            source_path = str(base["source_path"])
            if current.is_dir and (
                target_path == source_path or target_path.startswith(source_path + "/")
            ):
                raise GuangYaFSChangeError("不能把目录移动到自身或其子目录")
            target_name = str(base.get("new_name") or current.name)
            planned_names = planned_target_names.setdefault(target_path, set())
            if target_name.casefold() in planned_names:
                raise GuangYaFSChangeError("计划在移动目标中生成了重复名称")
            planned_names.add(target_name.casefold())
            if pending_target is None:
                target_items = _list_map(client, target_id, cache)
                if _name_conflict(target_items, target_name):
                    raise GuangYaFSChangeError("移动目标中已有同名对象")
            base.update(
                target_path=target_path,
                target_id=target_id,
                target_snapshot=target_snapshot,
            )
            if pending_target is not None:
                base["target_create_path"] = target_path
        frozen.append(base)

    structural_paths = [
        str(item.get("source_path") or "")
        for item in frozen
        if item.get("op") in {"move", "relocate", "trash"}
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

    counts = {
        key: 0 for key in ("rename", "move", "relocate", "trash", "create_directory")
    }
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
        elif op == "relocate":
            samples.append(
                f"移动并改名：{item['source']['name']} → "
                f"{Path(str(item['target_path'])).name or '根目录'} / {item['new_name']}"
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
    with _plan_state_lock():
        _atomic_write(plan)
        maintained = _maintain_fs_change_plans_unlocked(
            preserve_plan_id=str(plan["plan_id"])
        )
        if int(maintained.get("capacity_exceeded") or 0) > 0:
            _discard_fs_change_plan_unlocked(str(plan["plan_id"]))
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


def _target_id(
    item: dict[str, Any],
    created_targets: dict[str, str] | None,
    *,
    allow_pending: bool = False,
) -> str:
    create_path = str(item.get("target_create_path") or "")
    if not create_path:
        return str(item.get("target_id") or "0")
    created_id = str((created_targets or {}).get(create_path) or "")
    if created_id:
        return created_id
    if allow_pending:
        return ""
    raise GuangYaFSChangeStale("计划中的新建目标目录尚未就绪")


def _preflight_operation(
    client: GuangYaClient,
    item: dict[str, Any],
    *,
    created_targets: dict[str, str] | None = None,
    allow_pending_target: bool = False,
) -> None:
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
    elif op in {"move", "relocate"}:
        target_id = _target_id(
            item,
            created_targets,
            allow_pending=allow_pending_target,
        )
        if not target_id:
            return
        if not item.get("target_create_path") and not _verify_directory_snapshot(
            client, target_id, item.get("target_snapshot")
        ):
            raise GuangYaFSChangeStale("移动目标目录已变化，请重新预览")
        siblings = {str(row.file_id): row for row in client.list_dir(target_id)}
        target_name = str(
            item.get("new_name") if op == "relocate" else source.get("name") or ""
        )
        if _name_conflict(siblings, target_name):
            raise GuangYaFSChangeStale("移动目标中已有同名对象，请重新预览")


def _verify_after(
    client: GuangYaClient,
    item: dict[str, Any],
    created_id: str = "",
    *,
    created_targets: dict[str, str] | None = None,
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
    if op in {"move", "relocate"}:
        target_id = _target_id(item, created_targets)
        target_name = str(
            item.get("new_name") if op == "relocate" else source.get("name") or ""
        )
        return any(
            str(row.file_id) == file_id and row.name == target_name
            for row in client.list_dir(target_id)
        )
    return False


def update_fs_change_plan_execution(
    plan_id: str,
    *,
    status: str,
    execution: dict[str, Any],
    expected_statuses: set[str] | frozenset[str] | None = None,
    expected_job_id: str = "",
) -> dict[str, Any]:
    """在跨进程锁内执行带前置状态/job_id 的文件级 CAS。"""
    safe_status = str(status or "").strip().casefold()
    if safe_status not in {
        "confirmed",
        "queued",
        "running",
        *_TERMINAL_PLAN_STATUSES,
    }:
        raise GuangYaFSChangeError("光鸭变更计划目标状态无效")
    with _plan_state_lock():
        payload = _read(plan_id)
        current_status = str(payload.get("status") or "")
        if expected_statuses is not None and current_status not in expected_statuses:
            raise GuangYaFSChangeStale("光鸭变更计划状态已变化，请勿重复执行")
        safe_job_id = str(expected_job_id or "").strip().casefold()
        if safe_job_id and (
            not _SAFE_JOB_ID.fullmatch(safe_job_id)
            or not hmac.compare_digest(str(payload.get("job_id") or ""), safe_job_id)
        ):
            raise GuangYaFSChangeStale("光鸭变更计划任务绑定已变化")
        payload["status"] = safe_status
        payload["execution"] = dict(execution)
        payload["updated_at"] = _now_iso()
        _atomic_write(payload)
        return payload


def _claim_fs_change_plan_execution(
    plan_id: str,
    *,
    job_id: str,
    started_at: str,
    stats: dict[str, int],
) -> dict[str, Any]:
    safe_job_id = str(job_id or "").strip().casefold()
    if not _SAFE_JOB_ID.fullmatch(safe_job_id):
        raise GuangYaFSChangeError("光鸭变更任务编号无效")
    return update_fs_change_plan_execution(
        plan_id,
        status="running",
        execution={"started_at": started_at, **stats},
        expected_statuses={"queued"},
        expected_job_id=safe_job_id,
    )


def _operation_stat_key(operation: str) -> str:
    try:
        return {
            "rename": "renamed",
            "move": "moved",
            "relocate": "relocated",
            "trash": "trashed",
            "create_directory": "created",
        }[operation]
    except KeyError as exc:
        raise GuangYaFSChangeError("光鸭变更计划包含未知操作") from exc


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
    job_id = str(payload.get("job_id") or "").strip().casefold()
    if not _SAFE_JOB_ID.fullmatch(job_id):
        raise GuangYaFSChangeError("光鸭变更任务编号无效")
    plan = load_fs_change_plan(
        plan_id,
        expected_fingerprint=expected_fingerprint,
        require_confirmed=True,
    )
    if str(plan.get("status") or "") != "queued" or not hmac.compare_digest(
        str(plan.get("job_id") or ""), job_id
    ):
        raise GuangYaFSChangeStale("光鸭变更计划任务绑定已变化")
    if not hmac.compare_digest(
        str(plan.get("owner_digest") or ""), str(payload.get("owner_digest") or "")
    ):
        raise GuangYaFSChangeError("光鸭变更任务会话不匹配")
    raw_generation = payload.get("credential_generation")
    expected_generation = int(raw_generation) if raw_generation is not None else -1
    client = client_factory()
    stats = {
        "total": len(plan.get("operations") or []),
        "renamed": 0,
        "moved": 0,
        "relocated": 0,
        "trashed": 0,
        "created": 0,
        "failed": 0,
        "verification_failed": 0,
        "precondition_failed": 0,
        "audit_failures": 0,
    }
    started_at = _now_iso()
    persistence_uncertain = False
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
            _preflight_operation(client, item, allow_pending_target=True)
        # 预检日志先于 running CAS 写入；若日志介质不可用，此时尚未产生任何
        # provider 副作用，可以安全失败而不会制造“远端已写、本地 failed”。
        _append_journal(
            plan_id,
            {"action": "preflight", "status": "completed", "total": len(operations)},
        )
        _claim_fs_change_plan_execution(
            plan_id,
            job_id=job_id,
            started_at=started_at,
            stats=stats,
        )
        created_targets: dict[str, str] = {}
        for index, item in enumerate(operations, start=1):
            if cancel_check is not None:
                cancel_check()
            op = str(item.get("op") or "")
            source = item.get("source") if isinstance(item.get("source"), dict) else {}
            created_id = ""
            error_type = ""
            provider_code = ""
            provider_write_started = False
            stat_key = _operation_stat_key(op)
            try:
                _preflight_operation(
                    client,
                    item,
                    created_targets=created_targets,
                )
                provider_write_started = True
                if op == "rename":
                    client.rename(
                        str(source.get("file_id") or ""), str(item["new_name"])
                    )
                elif op == "move":
                    client.move(
                        [str(source.get("file_id") or "")],
                        _target_id(item, created_targets),
                    )
                elif op == "relocate":
                    client.rename(
                        str(source.get("file_id") or ""), str(item["new_name"])
                    )
                    client.move(
                        [str(source.get("file_id") or "")],
                        _target_id(item, created_targets),
                    )
                elif op == "trash":
                    execute_recycle_bin_delete(
                        client,
                        trigger="agent_guangya_fs_change",
                        reason="Agent 已确认的光鸭文件变更计划",
                        candidate=DeleteCandidate(
                            file_id=str(source.get("file_id") or ""),
                            name=str(source.get("name") or ""),
                            parent_id=str(source.get("parent_id") or "0"),
                            size=max(0, int(source.get("size") or 0)),
                            gcid=str(source.get("etag") or ""),
                        ),
                        safe_failure_message="光鸭对象移入回收站失败",
                    )
                elif op == "create_directory":
                    created_id = client.create_dir(
                        str(item["name"]), str(item.get("parent_id") or "0")
                    )
                else:  # _operation_stat_key 已阻止未知操作
                    raise GuangYaFSChangeError("光鸭变更计划包含未知操作")
                if not _verify_after(
                    client,
                    item,
                    created_id,
                    created_targets=created_targets,
                ):
                    stats["verification_failed"] += 1
                    raise GuangYaFSChangeError("写入后的云端状态校验失败")
                if op == "create_directory":
                    created_targets[str(item.get("created_path") or "")] = str(
                        created_id
                    )
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
            except Exception as exc:  # noqa: BLE001 - 单项失败需收束为可审计部分完成
                error_type = type(exc).__name__
                # provider 可能在连接中断前已经接受写入。若冻结后置条件成立，
                # 则不能再把它算成“失败后可重试”；trash 同时标记审计缺口。
                applied = False
                if provider_write_started:
                    try:
                        applied = _verify_after(
                            client,
                            item,
                            created_id,
                            created_targets=created_targets,
                        )
                    except Exception:  # noqa: BLE001 - 后置核验失败即保持未知
                        applied = False
                if applied:
                    if op == "create_directory":
                        created_targets[str(item.get("created_path") or "")] = str(
                            created_id
                        )
                    stats[stat_key] += 1
                    status = "completed"
                    if op == "trash":
                        stats["audit_failures"] += 1
                        persistence_uncertain = True
                else:
                    stats["failed"] += 1
                    if op == "relocate" and provider_write_started:
                        # Relocate is a two-call Provider operation. A transport failure
                        # between rename and move may leave a safe but partial remote state.
                        persistence_uncertain = True
                    status = "failed"
            try:
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
            except Exception as exc:  # noqa: BLE001 - 远端写后不得抛成普通 failed
                logger.error(
                    "光鸭变更远端写入后日志持久化失败 plan=%s index=%s type=%s",
                    plan_id,
                    index,
                    type(exc).__name__,
                )
                stats["audit_failures"] += 1
                persistence_uncertain = True
                # 日志介质失效后停止追加写入，避免扩大无法可靠追溯的副作用面。
                break
        successful = (
            stats["renamed"]
            + stats["moved"]
            + stats["relocated"]
            + stats["trashed"]
            + stats["created"]
        )
        partial = stats["failed"] > 0 or persistence_uncertain
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
        final_status = (
            "manual_review"
            if persistence_uncertain
            else "partial"
            if partial
            else "completed"
        )
        try:
            _append_journal(
                plan_id,
                {
                    "action": "finalize",
                    "status": final_status,
                    "successful": successful,
                    "failed": stats["failed"],
                    "audit_failures": stats["audit_failures"],
                },
            )
        except Exception as exc:  # noqa: BLE001 - 队列必须返回 partial/manual review
            logger.error(
                "光鸭变更终态日志持久化失败 plan=%s type=%s",
                plan_id,
                type(exc).__name__,
            )
            stats["audit_failures"] += 1
            persistence_uncertain = True
            partial = True
            final_status = "manual_review"
        try:
            update_fs_change_plan_execution(
                plan_id,
                status=final_status,
                execution={
                    "started_at": started_at,
                    "finished_at": finished_at,
                    **stats,
                },
                expected_statuses={"running"},
                expected_job_id=job_id,
            )
        except Exception as exc:  # noqa: BLE001 - 远端已写，返回 partial 而非可重试 failed
            logger.error(
                "光鸭变更终态计划持久化失败 plan=%s type=%s",
                plan_id,
                type(exc).__name__,
            )
            stats["audit_failures"] += 1
            persistence_uncertain = True
            partial = True
        return {
            "partial": partial or persistence_uncertain,
            "requires_manual": persistence_uncertain,
            "stats": stats,
        }
    finally:
        try:
            client.close()
        except Exception as exc:  # noqa: BLE001 - 关闭资源失败不能覆盖远端执行结果
            logger.warning("关闭光鸭文件变更客户端失败 type=%s", type(exc).__name__)
