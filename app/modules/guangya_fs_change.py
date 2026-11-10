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
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from app.clients.guangya import (
    GuangYaClient,
    GuangYaFile,
    GuangYaWriteRejected,
    guangya_provider_task_state,
    verify_guangya_write,
)
from app.config import PATHS
from app.modules.guangya_journal import append_guangya_journal
from app.modules.guangya_workspace import (
    GuangYaWorkspaceError,
    GuangYaWorkspaceStale,
    file_location,
    list_files,
    locate_file_in_roots,
    resolve_workspace_path,
)
from app.modules.organize_delete_audit import (
    DeleteCandidate,
    execute_recycle_bin_delete,
)
from app.modules.process_lock import CrossProcessLock
from app.modules.strm import cloud_change_sources, trigger_cloud_changes
from app.modules.web_secret import get_web_secret
from app.private_files import protect_private_file
from app.repositories.organize_operation_jobs import (
    get_organize_operation_job,
    organize_operation_owner_digest,
    organize_operation_queue_position,
    sanitize_organize_operation_result,
)

_PLAN_VERSION = 1
_PLAN_TTL_SECONDS = 10 * 60
_EXECUTE_TTL_SECONDS = 15 * 60
MAX_FS_CHANGE_INPUT_OPERATIONS = 200
MAX_FS_CHANGE_OBJECT_OPERATIONS = 200
MAX_FS_CHANGE_CREATE_OPERATIONS = 200
MAX_FS_CHANGE_BATCH_OPERATIONS = 2_000
MAX_FS_CHANGE_OPERATIONS = (
    MAX_FS_CHANGE_OBJECT_OPERATIONS + MAX_FS_CHANGE_CREATE_OPERATIONS
)
_MAX_PLAN_BYTES = 2 * 1024 * 1024
_COPY_FOREGROUND_ATTEMPTS = 21
_MAX_COPY_ITEMS = 2_000
_MAX_COPY_DIRS = 500
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

    operation_result: dict[str, Any]


class GuangYaFSCheckpointError(GuangYaFSChangeError):
    """阶段事实无法可靠落盘，必须停止追加写入。"""


class GuangYaFSCopyPending(GuangYaFSChangeError):
    """已提交的复制仍在运行：让出 worker，原任务稍后仅读对账再续行。"""

    def __init__(self, position: int = 0):
        super().__init__("光鸭复制仍在处理中，完成后将继续原计划")
        self.position = position


class GuangYaFSChangeStale(GuangYaFSChangeError):
    """冻结计划已过期或云端对象已变化。"""


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _directory() -> Path:
    return Path(PATHS.data_dir) / "agent-guangya-fs-change"


@contextmanager
def _plan_state_lock() -> Generator[None, None, None]:
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
    if not _SAFE_PLAN_ID.fullmatch(plan_id or ""):
        raise GuangYaFSChangeError("光鸭变更计划编号无效")
    return _directory() / f"{plan_id}.json"


def _journal_path(plan_id: str) -> Path:
    if not _SAFE_PLAN_ID.fullmatch(plan_id or ""):
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
    secret = (get_web_secret() or "").encode("utf-8")
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
        # 仅校验升级前已经签名的业务job；新计划不再生产/读取观察句柄。
        "observation_ref": payload.get("observation_ref"),
        "trigger_strm": payload.get("trigger_strm"),
        "operations": payload.get("operations"),
    }
    if "batch_summary" in payload:
        stable["batch_summary"] = payload["batch_summary"]
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


def inspect_fs_change_plan_recovery(
    payload: dict[str, Any], *, job_id: str, owner_digest: str, lease_generation: int,
) -> dict[str, Any]:
    """仅读同一签名检查点，恢复与取消共用；None 表示无法排除已发送复制。"""
    unknown = {"resumable": False, "copy_in_flight": None}
    try:
        plan = _read(str(payload.get("plan_id") or ""))
        _validate_plan_identity(plan, expected_fingerprint=str(payload.get("plan_fingerprint") or ""))
        if (plan.get("job_id") != job_id or plan.get("owner_digest") != owner_digest
                or payload.get("owner_digest") != owner_digest):
            return unknown
        if plan.get("status") == "queued":
            return {"resumable": True, "copy_in_flight": False}
        execution: dict[str, Any] = plan.get("execution") or {}
        if plan.get("status") == "running" and execution.get("checkpoint_version") == 2:
            execution = _read_fs_checkpoints(plan)
        resumable = bool(
            plan.get("status") in {"running", "completed", "partial"}
            and execution.get("checkpoint_version") == 2
            and 0 < int(execution.get("lease_generation") or 0) <= lease_generation
            and len(execution.get("operation_items") or []) == len(plan.get("operations") or [])
        )
        if not resumable:
            return unknown
        copies = execution.get("copy_tasks") or {}
        active = any(
            item["op"] == "copy" and (
                outcome["status"] in {"unknown", "partial"}
                or (str(index) in copies and outcome["status"] not in {"completed", "failed", "blocked"})
            )
            for index, (item, outcome) in enumerate(zip(plan["operations"], execution["operation_items"]), 1)
        )
        return {"resumable": True, "copy_in_flight": active}
    except (GuangYaFSChangeError, TypeError, ValueError, KeyError):
        return unknown


def _append_journal(plan_id: str, event: dict[str, Any]) -> None:
    path = _journal_path(plan_id)
    _ensure_private_directory(path.parent)
    append_guangya_journal(path, {"at": _now_iso(), **event})


def _owner_digest(owner: str) -> str:
    owner_key = (owner or "").strip()
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
        _owner_digest(owner) if owner is not None else (owner_digest or "")
    )
    if expected_owner and not hmac.compare_digest(
        str(payload.get("owner_digest") or ""), expected_owner
    ):
        raise GuangYaFSChangeError("光鸭变更计划不属于当前会话")
    if expected_fingerprint and not hmac.compare_digest(
        str(payload.get("fingerprint") or ""), (expected_fingerprint or "")
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


def _fs_change_task(plan: dict[str, Any]) -> dict[str, Any] | None:
    job_id = str(plan.get("job_id") or "")
    row = get_organize_operation_job(job_id) if _SAFE_JOB_ID.fullmatch(job_id) else None
    if row is None:
        return None
    status = str(row["status"] or "")
    position = organize_operation_queue_position(job_id) if status == "pending" else 0
    if status == "pending" and position == 0:
        # 队列位置读取会收束已过期任务；回执必须采用收束后的真实状态。
        row = get_organize_operation_job(job_id)
        if row is None:
            return None
        status = str(row["status"] or "")
    return {
        "ok": True,
        "task_id": job_id,
        "status": status,
        "expires_at": float(row["expires_at"]),
        "cancel_requested": bool(row["cancel_requested"]),
        "queued": status == "pending",
        "queue_position": position,
        "replayed": True,
    }


def submit_fs_change_plan(
    plan_id: str,
    *,
    owner: str,
    expected_fingerprint: str,
    on_admitted: Callable[..., None] | None = None,
) -> dict[str, Any]:
    """确认既有冻结计划并提交原持久队列；重试复用同一任务及执行检查点。"""
    if not isinstance(expected_fingerprint, str) or not re.fullmatch(
        r"[0-9a-f]{64}", expected_fingerprint
    ):
        raise GuangYaFSChangeError("光鸭变更计划指纹无效")
    plan = load_fs_change_plan(
        plan_id, owner=owner, expected_fingerprint=expected_fingerprint
    )
    existing = _fs_change_task(plan)
    if plan.get("job_id") and existing is None:
        raise GuangYaFSChangeStale(
            "光鸭变更已绑定任务但记录不可用，请先核验原任务，不能重复提交"
        )
    if existing is not None and existing["status"] in _TERMINAL_PLAN_STATUSES:
        return existing
    if str(plan.get("status") or "") in {"previewed", "confirmed"}:
        try:
            plan = confirm_fs_change_plan(
                plan_id, owner=owner, expected_fingerprint=expected_fingerprint
            )
        except GuangYaFSChangeStale:
            # 并发提交可能已把同一冻结计划绑定入队；只复用既有任务。
            plan = load_fs_change_plan(
                plan_id, owner=owner, expected_fingerprint=expected_fingerprint
            )
            existing = _fs_change_task(plan)
            if existing is None:
                raise
            if existing["status"] in _TERMINAL_PLAN_STATUSES:
                return existing
    elif str(plan.get("status") or "") not in {"queued", "running"}:
        raise GuangYaFSChangeStale("光鸭变更计划已执行或不再可排队")
    if existing is not None and time.time() >= existing["expires_at"]:
        raise GuangYaFSChangeStale(
            "光鸭原任务期限已到，请先核验原任务结果，不能重复提交"
        )
    from app.modules.organize_tasks import get_organize_manager

    task = get_organize_manager().start_durable_operation(
        "光鸭文件变更",
        "已确认通用文件变更冻结计划",
        job_kind="agent_guangya_fs_change",
        owner=owner,
        payload={
            "version": 1,
            "plan_id": plan_id,
            "plan_fingerprint": expected_fingerprint,
            "owner_digest": plan["owner_digest"],
            "credential_generation": int(plan["credential_generation"]),
        },
        dedupe_key=f"agent-guangya-fs-change:{plan_id}:{expected_fingerprint}",
        **(
            {"ttl_seconds": 6 * 60 * 60}
            if plan.get("batch_summary")
            and plan["stats"]["total"] > MAX_FS_CHANGE_OBJECT_OPERATIONS
            else {}
        ),
        on_admitted=on_admitted,
    )
    try:
        persisted = load_fs_change_plan(
            plan_id, owner=owner, expected_fingerprint=expected_fingerprint
        )
        receipt = _fs_change_task(persisted)
    except Exception as exc:
        raise GuangYaFSChangeError(
            "光鸭操作队列状态当前不可用，请稍后查询任务状态"
        ) from exc
    if receipt is not None and (
        task.get("ok") or receipt["status"] in {"pending", "running"}
    ):
        return {
            **receipt,
            "replayed": bool(task.get("replayed")) if task.get("ok") else True,
        }
    if task.get("ok"):
        return task
    raise GuangYaFSChangeError("光鸭操作未能可靠入队，请重新生成预览后重试")


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
    safe_job_id = (job_id or "").strip().casefold()
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
    safe_job_id = (job_id or "").strip().casefold()
    safe_queue_status = (queue_status or "").strip().casefold()
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
            or max(0, (audit_failures or 0)) > 0
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
                "queue_error_code": (error_code or "")[:80],
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
    preserved = (preserve_plan_id or "").strip().casefold()
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
        "file_id": item.file_id,
        "parent_id": item.parent_id,
        "name": item.name,
        "is_dir": item.is_dir,
        "size": max(0, (item.size or 0)),
        "etag": (item.etag or ""),
        "updated_at": max(0, (item.updated_at or 0)),
    }


def _snapshot_matches(item: GuangYaFile | None, snapshot: dict[str, Any], *, exact_updated_at: bool = False) -> bool:
    if item is None:
        return False
    if not (
        item.file_id == str(snapshot.get("file_id") or "")
        and item.parent_id == str(snapshot.get("parent_id") or "0")
        and item.name == str(snapshot.get("name") or "")
        and item.is_dir == bool(snapshot.get("is_dir"))
        and max(0, (item.size or 0)) == max(0, int(snapshot.get("size") or 0))
        and (item.etag or "") == str(snapshot.get("etag") or "")
    ):
        return False
    expected_updated = max(0, int(snapshot.get("updated_at") or 0))
    if exact_updated_at:
        return max(0, (item.updated_at or 0)) == expected_updated
    return (
        bool(item.etag)
        or not expected_updated
        or max(0, (item.updated_at or 0)) == expected_updated
    )


def _full_path(parent_path: str, name: str) -> str:
    return (parent_path.rstrip("/") + "/" + name) if parent_path != "/" else "/" + name


def _resolve_directory(
    client: GuangYaClient, path: str, observed_directories: dict[str, dict[str, Any]]
) -> tuple[str, dict[str, Any] | None]:
    normalized = _normalize_path(path)
    if normalized == "/":
        return "0", None
    error = (f"目录路径解析失败：{normalized}；target_path/parent_path 必须是目录路径，"
             "目标文件名请使用 new_name；本次预检未执行云端写入")
    try:
        target = resolve_workspace_path(client, normalized)
    except GuangYaWorkspaceError as exc:
        raise type(exc)(f"{error}。{exc}") from exc
    if not target.is_dir:
        raise GuangYaFSChangeError(error)
    observed = observed_directories.get(normalized)
    if observed is None:
        matches = [item for path, item in observed_directories.items() if path.casefold() == normalized.casefold()]
        # 跟随解析器实际选中的父目录和名称，不能把真实共存的 D/d 合并。
        identified = [item for item in matches if item["parent_id"] == target.parent_id and item["name"] == target.name]
        if len(identified or matches) > 1:
            raise GuangYaFSChangeStale("目标目录观察存在歧义，请重新读取后再生成计划")
        observed = next(iter(identified or matches), None)
    if observed is not None and not _snapshot_matches(target, observed):
        raise GuangYaFSChangeStale("目标目录与已观察对象不一致，请重新读取后再生成计划")
    return target.file_id, _snapshot(target)


def _list_map(
    client: GuangYaClient,
    parent_id: str,
    cache: dict[str, dict[str, GuangYaFile]],
) -> dict[str, GuangYaFile]:
    if parent_id not in cache:
        cache[parent_id] = {
            item.file_id: item for item in client.list_dir(parent_id)
        }
    return cache[parent_id]


def _name_conflict(
    items: dict[str, GuangYaFile], name: str, *, exclude_id: str = ""
) -> bool:
    folded = name.casefold()
    return any(
        item.name.casefold() == folded and item.file_id != (exclude_id or "")
        for item in items.values()
    )




def _source_key(operation: dict[str, Any]) -> str:
    file_id = operation.get("file_id")
    if not isinstance(file_id, str) or not file_id.strip() or file_id.strip() == "0":
        raise GuangYaFSChangeError("必须使用真实file_id，不能变更账号根目录")
    return file_id.strip()


def _read_fs_change_sources(
    client: GuangYaClient, operations: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """凭真实身份读取源对象，复用公共寻址核对父链，不创建观察引用或会话状态。"""
    if not client.logged_in:
        raise GuangYaFSChangeError("光鸭账号尚未连接")
    fields = {
        "rename": {"op", "file_id", "new_name"},
        "move": {"op", "file_id", "target_path"},
        "relocate": {"op", "file_id", "target_path", "new_name"},
        "copy": {"op", "file_id", "target_path"},
        "trash": {"op", "file_id"},
        "create_directory": {"op", "parent_path", "name"},
    }
    for raw in operations:
        if not isinstance(raw, dict):
            raise GuangYaFSChangeError("光鸭变更操作格式无效")
        op = str(raw.get("op") or "").strip().casefold()
        expected = fields.get(op)
        if expected is None:
            raise GuangYaFSChangeError("真实身份变更操作不支持该 op")
        allowed = expected | ({"new_name"} if op == "copy" else set())
        allowed |= {"parent_id"} if op == "create_directory" else {"target_id"} if op in {"move", "copy", "relocate"} else set()
        for key in {"parent_id", "target_id"}.intersection(raw):
            if not isinstance(raw[key], str) or not raw[key].strip():
                raise GuangYaFSChangeError(key + "必须是真实目录ID")
        if op != "create_directory":
            allowed |= {"expected", "expected_tree", "require_empty"}
        if set(raw) - allowed or expected - set(raw):
            raise GuangYaFSChangeError(
                "真实身份变更操作字段无效；请使用 file_id 与对应操作参数"
            )
        if "expected" in raw and not isinstance(raw["expected"], dict):
            raise GuangYaFSChangeError("expected必须为读取时的对象快照")
        if "require_empty" in raw and (op != "trash" or type(raw["require_empty"]) is not bool):
            raise GuangYaFSChangeError("require_empty仅用于目录回收且必须是布尔值")
        if "expected_tree" in raw and (not isinstance(raw["expected_tree"], list) or not raw["expected_tree"]):
            raise GuangYaFSChangeError("expected_tree必须包含完整目录清单")
    cache: dict[str, GuangYaFile | None] = {}
    entries = {}
    for raw in operations:
        if str(raw["op"]).strip().casefold() == "create_directory":
            continue
        file_id = _source_key(raw)
        if file_id in entries:
            continue
        current = client.file_info(file_id)
        cache[file_id] = current
        if current is None or current.file_id != file_id:
            raise GuangYaFSChangeStale("光鸭源对象不存在或身份已变化")
        entry = locate_file_in_roots(
            client,
            {"file_id": file_id, "is_dir": current.is_dir},
            [{"file_id": "0", "path": "/", "is_dir": True}],
            cache=cache,
            check_cancelled=lambda: None,
        )
        if entry is None:
            raise GuangYaFSChangeStale(
                "无法按真实身份完整核对光鸭源对象路径，请重新核对目录"
            )
        entries[file_id] = entry
    return entries


def _compile_operations(
    operations: list[dict[str, Any]], entries: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    expanded = operations
    # 未来路径描述的是同一个已观察目录，不是要创建的同名替代目录。
    # 在唯一冻结链路中归一为当前路径；执行阶段仍只使用核验后的目录 ID。
    destinations: dict[str, str] = {}
    for raw in expanded:
        op = str(raw.get("op") or "").strip().casefold()
        if op not in {"move", "relocate", "rename"}:
            continue
        observed = entries.get(_source_key(raw), {})
        if not observed.get("is_dir"):
            continue
        parent = str(observed.get("parent_path") or "/")
        source = _full_path(parent, str(observed["name"]))
        destination = _full_path(
            parent if op == "rename" else _normalize_path(raw.get("target_path")),
            _validate_name(raw.get("new_name")) if op != "move" else str(observed["name"]),
        )
        if destination == source:
            continue
        if destination in destinations and destinations[destination] != source:
            raise GuangYaFSChangeError("计划中的目录目标路径不唯一")
        destinations[destination] = source

    compiled = []
    for raw in expanded:
        item = dict(raw)
        op = str(raw.get("op") or "").strip().casefold()
        fields = ("parent_path",) if op == "create_directory" else ("target_path",) if op in {"move", "relocate", "copy"} else ()
        for field in fields:
            path = _normalize_path(raw.get(field))
            used = set()
            while True:
                matches = [target for target in destinations if path == target or path.startswith(target + "/")]
                if not matches:
                    break
                target = max(matches, key=len)
                if target in used:
                    raise GuangYaFSChangeError("计划中的目录路径变更存在循环或歧义")
                used.add(target)
                path = _normalize_path(destinations[target] + path[len(target):])
            item[field] = path
        compiled.append(item)
    return compiled


def _validate_plan_names(operations: list[dict[str, Any]]) -> None:
    """按实际目录身份统一检查计划内占用；跨目录搬移的临时改名不留占位。"""
    occupied: dict[tuple[tuple[str, str], str], str] = {}
    for item in operations:
        op = str(item["op"])
        if op == "trash":
            continue
        source = item.get("source") or {}
        name = str(
            item.get("name") if op == "create_directory"
            else item.get("new_name", source.get("name", ""))
        ).casefold()
        if op == "rename":
            directory = ("id", str(source.get("parent_id") or "0"))
        else:
            role = "parent" if op == "create_directory" else "target"
            created_path = str(item.get(f"{role}_create_path") or "")
            directory = (("created", created_path) if created_path
                         else ("id", str(item.get(f"{role}_id") or "0")))
        key = (directory, name)
        previous = occupied.get(key)
        # 同时核对规整操作的中间名称；中间占位完成后释放，不当成最终目标。
        transient = ((directory, str(source.get("name") or "").casefold()) if item.get("move_first") or op == "copy"
                     else (("id", str(source.get("parent_id") or "0")), name))
        if previous is not None or (
            op in {"relocate", "copy"} and item.get("new_name") and item["new_name"] != source.get("name")
            and transient in occupied
        ):
            raise GuangYaFSChangeError(
                "计划不能重复新建同名目录" if previous == op == "create_directory"
                else "计划在目标目录中生成了重复名称"
            )
        occupied[key] = op


def _operation_label(item: dict[str, Any]) -> str:
    op = str(item["op"])
    name = str((item.get("source") or {}).get("name") or item.get("name") or "")
    label = {"rename": "改名", "move": "移动", "relocate": "移动并改名",
             "copy": "复制", "trash": "移入回收站", "create_directory": "新建目录"}[op]
    if op == "rename":
        return f"{label}：{name} → {item['new_name']}"
    if op in {"move", "relocate", "copy"}:
        suffix = f" / {item['new_name']}" if item.get("new_name") else ""
        return f"{label}：{name} → {_short_path(str(item['target_path']))}{suffix}"
    if op == "create_directory" and item.get("parent_path"):
        return f"{label}：{_short_path(_full_path(str(item['parent_path']), name))}"
    return f"{label}：{name}"


def _short_path(path: str) -> str:
    # 卡片只显示末两级：同名“Season 01”必须能看出建在哪个父目录下。
    parts = [part for part in path.strip("/").split("/") if part]
    return " › ".join(parts[-2:]) or "账号根目录"


def _batch_filter_label(value: dict[str, Any]) -> str:
    if not value:
        return "全部剩余文件"
    labels = []
    if "size_lt_bytes" in value:
        labels.append(f"大小 < {value['size_lt_bytes']} 字节")
    if "size_gte_bytes" in value:
        labels.append(f"大小 ≥ {value['size_gte_bytes']} 字节")
    for key, label in (("kinds", "类型"), ("extensions", "扩展名"), ("name_contains", "名称包含")):
        if value.get(key):
            labels.append(f"{label}=" + "、".join(value[key]))
    return " 且 ".join(labels)


def _batch_matches(entry: dict[str, Any], rule: dict[str, Any]) -> bool:
    size = int(entry.get("size") or 0)
    return (
        ("size_lt_bytes" not in rule or size < rule["size_lt_bytes"])
        and ("size_gte_bytes" not in rule or size >= rule["size_gte_bytes"])
        and (not rule.get("kinds") or entry["media_kind"] in rule["kinds"])
        and (not rule.get("extensions") or str(entry.get("extension") or Path(str(entry.get("name") or "").replace("。", ".").replace("．", ".")).suffix.lstrip(".")).casefold() in rule["extensions"])
        and (not rule.get("name_contains") or any(word in entry["name"].casefold() for word in rule["name_contains"]))
    )


def _batch_unique_name(name: str, parent_path: str, used: set[str], siblings: set[str]) -> str:
    if name.casefold() not in used:
        used.add(name.casefold())
        return name
    suffix = Path(name).suffix
    stem = name[:-len(suffix)] if suffix else name
    source = re.sub(r"[^\w.()\[\] -]+", "_", Path(parent_path).name).strip(" ._")[:48] or "来源"
    number = 1
    blocked = used | siblings
    while True:
        tag = f" ({source})" + (f"-{number}" if number > 1 else "")
        budget = 255 - len(tag) - len(suffix)
        # 长文件名仍保留末尾分片/版本标记及原始扩展名。
        base = stem if len(stem) <= budget else stem[:budget - 17] + "…" + stem[-16:]
        candidate = base + tag + suffix
        if candidate.casefold() not in blocked:
            used.add(candidate.casefold())
            return _validate_name(candidate)
        number += 1


def _file_filter_arguments(value: object, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {
        "size_lt_bytes", "size_gte_bytes", "kinds", "extensions", "name_contains",
    }:
        raise GuangYaFSChangeError(f"{field} 文件筛选条件无效")
    result: dict[str, Any] = {}
    for key, raw in value.items():
        if key in {"size_lt_bytes", "size_gte_bytes"}:
            if type(raw) is not int or not 0 <= raw <= 2**53 - 1:
                raise GuangYaFSChangeError(f"{field}.{key} 必须是非负整数字节数")
            result[key] = raw
            continue
        if not isinstance(raw, list) or not 1 <= len(raw) <= 16:
            raise GuangYaFSChangeError(f"{field}.{key} 必须包含 1 到 16 项")
        if any(not isinstance(item, str) or not 1 <= len(item.strip()) <= 160 for item in raw):
            raise GuangYaFSChangeError(f"{field}.{key} 必须是非空字符串列表")
        values = list(dict.fromkeys(item.strip().casefold() for item in raw))
        if key == "kinds" and set(values) - {"video", "subtitle", "image", "metadata", "other"}:
            raise GuangYaFSChangeError("批量规则只筛选文件，kinds 不支持目录")
        if key == "extensions" and any(not re.fullmatch(r"[a-z0-9]{1,16}", item) for item in values):
            raise GuangYaFSChangeError("extensions 必须是无点的扩展名，如 mp4、srt、torrent")
        result[key] = values
    if "size_lt_bytes" in result and result.get("size_gte_bytes", -1) >= result["size_lt_bytes"]:
        raise GuangYaFSChangeError("大小筛选下限必须小于上限")
    return result

def _batch_files_arguments(raw: dict[str, Any]) -> dict[str, Any]:
    if set(raw) - {
        "op", "file_id", "trash_filter", "target_path", "move_filter", "cleanup_empty_dirs", "conflict",
    }:
        raise GuangYaFSChangeError("batch_files 包含不支持的参数")
    file_id = _source_key(raw)
    cleanup = raw.get("cleanup_empty_dirs", False)
    if type(cleanup) is not bool:
        raise GuangYaFSChangeError("cleanup_empty_dirs 必须是布尔值")
    conflict = raw.get("conflict", "keep_both")
    if conflict not in ("keep_both", "error"):
        raise GuangYaFSChangeError("conflict 仅支持 keep_both 或 error，不允许覆盖")
    result: dict[str, Any] = {
        "op": "batch_files", "file_id": file_id,
        "cleanup_empty_dirs": cleanup, "conflict": conflict,
    }
    if "trash_filter" in raw:
        result["trash_filter"] = _file_filter_arguments(raw["trash_filter"], field="trash_filter")
        if not result["trash_filter"]:
            raise GuangYaFSChangeError("回收文件必须提供明确筛选条件，不能使用空 trash_filter")
    if "target_path" in raw:
        if not isinstance(raw["target_path"], str):
            raise GuangYaFSChangeError("target_path 必须是目录路径字符串")
        result["target_path"] = _normalize_path(raw["target_path"])
        result["move_filter"] = _file_filter_arguments(raw.get("move_filter", {}), field="move_filter")
    elif "move_filter" in raw:
        raise GuangYaFSChangeError("move_filter 必须配合 target_path 使用")
    if not cleanup and "trash_filter" not in result and "target_path" not in result:
        raise GuangYaFSChangeError("batch_files 至少指定回收条件、移动目标或空目录清理")
    return result

def _batch_prepare(client: GuangYaClient, rule: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], str]:
    """原批量规则按真实目录ID完整展开；沿用唯一fs-change计划，不保存观察。"""
    scope = file_location(client, rule["file_id"])
    if not scope["is_dir"]:
        raise GuangYaFSChangeError("batch_files必须指定真实的非根目录ID")
    root_path = scope["path"]
    root = scope
    try:
        fresh = list_files(client, scope["file_id"], recursive=True, max_items=MAX_FS_CHANGE_BATCH_OPERATIONS, max_depth=12)
    except GuangYaWorkspaceError as exc:
        raise GuangYaFSChangeError("目录扫描不完整；请拆分子目录后重新读取") from exc
    if fresh["scanned_dirs"] > 500:
        raise GuangYaFSChangeError("目录扫描不完整：目录数量超过批量规则预算")
    entries = fresh["items"]
    by_id = {entry["file_id"]: entry for entry in entries}
    directories = {entry["file_id"]: entry for entry in entries if entry["is_dir"]}
    directories[root["file_id"]] = fresh["root"]
    # scanner 的深度上限不记为 truncated；目录数量必须证明每个子目录都已读取。
    if (not fresh["scan_complete"] or fresh["scanned_dirs"] != len(directories)
            or fresh["scanned_items"] != len(entries) or len(by_id) != len(entries)
            or root["file_id"] in by_id):
        raise GuangYaFSChangeError("目录扫描不完整、超过范围上限或清单发生变化；请拆分子目录重新读取")
    paths = {file_id: _full_path(entry["parent_path"], entry["name"])
             for file_id, entry in directories.items()}
    children: dict[str, list[dict[str, Any]]] = {file_id: [] for file_id in directories}
    for entry in entries:
        if paths.get(entry["parent_id"]) != entry["parent_path"]:
            raise GuangYaFSChangeError("目录扫描父子关系发生变化，请重新读取")
        children[entry["parent_id"]].append(entry)
    target_path = rule.get("target_path", "")
    observed_directories = {paths[file_id]: entry for file_id, entry in directories.items()}
    target_id = _resolve_directory(client, target_path, observed_directories)[0] if target_path else ""
    used = {item.name.casefold() for item in client.list_dir(target_id)} if target_id else set()
    operations: list[dict[str, Any]] = []
    vacated: set[str] = set()
    trash_count = move_count = renamed_count = unchanged_count = file_count = cleanup_count = 0
    for entry in sorted(entries, key=lambda item: (item["parent_path"].casefold(), item["name"].casefold(), item["file_id"])):
        if entry["is_dir"]:
            continue
        file_count += 1
        file_id, name = entry["file_id"], entry["name"]
        if rule.get("trash_filter") and _batch_matches(entry, rule["trash_filter"]):
            operations.append({"op": "trash", "file_id": entry["file_id"]})
            vacated.add(file_id)
            trash_count += 1
        elif target_id and entry["parent_id"] != target_id and _batch_matches(entry, rule.get("move_filter", {})):
            if name.casefold() in used and rule.get("conflict") == "error":
                raise GuangYaFSChangeError("目标目录或批内已有同名对象")
            siblings = {item["name"].casefold() for item in children[entry["parent_id"]]}
            new_name = _batch_unique_name(name, entry["parent_path"], used, siblings)
            operation = {"op": "move", "file_id": entry["file_id"], "target_path": target_path}
            if new_name != name:
                operation.update(op="relocate", new_name=new_name)
                renamed_count += 1
            operations.append(operation)
            vacated.add(file_id)
            move_count += 1
        else:
            unchanged_count += 1
    if rule.get("cleanup_empty_dirs"):
        protected = {root["file_id"]}
        current = target_id
        while current in by_id and current not in protected:
            protected.add(current)
            current = by_id[current]["parent_id"]
        # 只清理冻结清单中已安排移出/回收全部子项的目录；目标与范围根始终保留。
        for file_id in sorted(directories, key=lambda item: (-paths[item].count("/"), item)):
            if file_id not in protected and all(child["file_id"] in vacated for child in children[file_id]):
                operations.append({"op": "trash", "file_id": file_id})
                vacated.add(file_id)
                cleanup_count += 1
    summary = (
        f"范围：{' › '.join(root_path.strip('/').split('/'))}；"
        f"回收规则：{_batch_filter_label(rule['trash_filter']) if rule.get('trash_filter') else '无'}；"
        f"移动规则：{_batch_filter_label(rule.get('move_filter', {})) if target_path else '无'}；"
        f"目标：{' › '.join(target_path.strip('/').split('/')) or '账号根目录' if target_path else '无'}；"
        f"扫描 {file_count} 个文件、{len(directories)} 个目录；"
        f"回收文件 {trash_count}、移动 {move_count}、重名保留两份 {renamed_count}、"
        f"清理空子目录 {cleanup_count}、其余保留 {unchanged_count}。"
    )
    return operations, {entry["file_id"]: entry for entry in entries}, summary

def build_fs_change_plan(
    client: GuangYaClient,
    *,
    owner: str,
    operations: list[dict[str, Any]],
    trigger_strm: bool = True,
) -> dict[str, Any]:
    """按真实ID冻结完整操作；原任务执行与检查点不变。"""
    _owner_digest(owner)
    if not client.logged_in:
        raise GuangYaFSChangeError("光鸭账号尚未连接")
    if type(trigger_strm) is not bool:
        raise GuangYaFSChangeError("trigger_strm必须是布尔值")
    if not isinstance(operations, list) or not operations or any(not isinstance(item, dict) for item in operations):
        raise GuangYaFSChangeError("必须提交完整的operations对象数组")
    batch_rule = None
    batch_summary = ""
    if any(str(raw.get("op") or "").strip().casefold() == "batch_files" for raw in operations):
        if len(operations) != 1:
            raise GuangYaFSChangeError("batch_files必须独占一个计划")
        batch_rule = _batch_files_arguments(operations[0])
        operations, entries, batch_summary = _batch_prepare(client, batch_rule)
    else:
        if len(operations) > MAX_FS_CHANGE_OPERATIONS:
            raise GuangYaFSChangeError("光鸭变更操作数量超过冻结计划上限")
        entries = _read_fs_change_sources(client, operations)
    operations = _compile_operations(operations, entries)
    observed_directories = {_full_path(str(item.get("parent_path") or "/"), str(item["name"])): item
                            for item in entries.values() if item.get("is_dir")}
    create_count = sum(
        1
        for item in operations
        if str(item.get("op") or "").strip().casefold() == "create_directory"
    )
    object_count = len(operations) - create_count
    operation_limit = (
        MAX_FS_CHANGE_BATCH_OPERATIONS if batch_rule is not None
        else MAX_FS_CHANGE_OPERATIONS
    )
    object_limit = (
        MAX_FS_CHANGE_BATCH_OPERATIONS if batch_rule is not None
        else MAX_FS_CHANGE_OBJECT_OPERATIONS
    )
    if (
        batch_rule is None and not 1 <= len(operations) <= operation_limit
        or object_count > object_limit
        or create_count > MAX_FS_CHANGE_CREATE_OPERATIONS
    ):
        raise GuangYaFSChangeError(
            "展开后的光鸭变更计划最多包含 "
            f"{MAX_FS_CHANGE_OBJECT_OPERATIONS} 项对象变更和 "
            f"{MAX_FS_CHANGE_CREATE_OPERATIONS} 项目录创建"
        )
    # 先创建浅层目录，再创建依赖它的子目录；对象操作保持原相对顺序。
    operations = sorted(operations, key=lambda item: (
        0, len(Path(item["parent_path"]).parts),
    ) if str(item.get("op") or "").strip().casefold() == "create_directory" else (1, 0))
    cache: dict[str, dict[str, GuangYaFile]] = {}
    resolved_directories: dict[str, tuple[str, dict[str, Any] | None]] = {}

    def resolve_directory(path: str) -> tuple[str, dict[str, Any] | None]:
        # 同一只读编译使用同一目标快照；实际执行仍逐项重新核验。
        if path not in resolved_directories:
            resolved_directories[path] = _resolve_directory(client, path, observed_directories)
        return resolved_directories[path]

    frozen: list[dict[str, Any]] = []
    seen_objects: set[str] = set()
    unchanged = 0
    pending_targets: dict[str, dict[str, Any]] = {}

    for raw in operations:
        if not isinstance(raw, dict):
            raise GuangYaFSChangeError("光鸭变更操作格式无效")
        op = str(raw.get("op") or "").strip().casefold()
        if op not in {
            "rename",
            "move",
            "relocate",
            "copy",
            "trash",
            "create_directory",
        }:
            raise GuangYaFSChangeError("不支持的光鸭变更操作")
        if op == "create_directory":
            parent_path = raw["parent_path"]
            name = _validate_name(raw.get("name"))
            pending_parent = pending_targets.get(parent_path)
            parent_id, parent_snapshot = (
                ("", None) if pending_parent is not None
                else resolve_directory(parent_path)
            )
            if "parent_id" in raw and (pending_parent is not None or raw["parent_id"] != parent_id):
                raise GuangYaFSChangeStale("已读取的父目录身份已变化，不能以同名目录替代")
            if pending_parent is None and _name_conflict(_list_map(client, parent_id, cache), name):
                raise GuangYaFSChangeError("新建目录名称已被占用")
            created_path = _full_path(parent_path, name)
            created = {
                "op": op,
                "parent_path": parent_path,
                "parent_id": parent_id,
                "parent_snapshot": parent_snapshot,
                "name": name,
                "created_path": created_path,
            }
            if pending_parent is not None:
                created["parent_create_path"] = parent_path
            pending_targets[created_path] = created
            frozen.append(created)
            continue

        handle = _source_key(raw)
        if handle not in entries:
            raise GuangYaFSChangeError("计划包含当前观察中不存在的对象引用")
        observed = entries[handle]
        parent_id = str(observed.get("parent_id") or "0")
        current = _list_map(client, parent_id, cache).get(
            str(observed.get("file_id") or "")
        )
        if not _snapshot_matches(current, observed):
            raise GuangYaWorkspaceStale("光鸭对象已变化，请重新读取后再生成计划")
        if current is None:
            raise GuangYaWorkspaceStale("光鸭对象已变化，请重新读取后再生成计划")
        base: dict[str, Any] = {
            "op": op,
            "source": _snapshot(current),
            "source_parent_path": str(observed.get("parent_path") or "/"),
            "source_path": _full_path(
                str(observed.get("parent_path") or "/"), current.name
            ),
        }
        if "expected" in raw:
            expected = raw["expected"]
            if expected.get("path") and expected["path"] != base["source_path"]:
                raise GuangYaFSChangeStale("源对象所在路径已变化，请重新读取完整目录")
            if not _snapshot_matches(current, expected):
                raise GuangYaFSChangeStale("对象已不符合读取时的名称、版本或父目录，请重新预览")
        if raw.get("require_empty"):
            if (not current.is_dir or not (current.etag or current.updated_at)
                    or not callable(getattr(client, "delete_empty_directory", None))
                    or getattr(client, "supports_guarded_empty_directory_delete", None) is False):
                raise GuangYaFSChangeError("真空目录回收要求可验证版本及原子空目录保护")
            base["require_empty"] = True
        if "expected_tree" in raw:
            if not current.is_dir:
                raise GuangYaFSChangeError("完整目录守卫只能用于目录")
            base["expected_tree"] = raw["expected_tree"]
            if not _guard_tree_matches(client, _snapshot(current), raw["expected_tree"]):
                raise GuangYaFSChangeStale("目录内容已不符合预览，未冻结任何写操作")
        if batch_rule is not None and op == "trash" and current.is_dir:
            base["require_empty"] = True
        if raw.get("require_empty") and client.list_dir(current.file_id):
            raise GuangYaFSChangeStale("目录已不为空，不能作为真空目录回收")
        if op in {"rename", "relocate"} or op == "copy" and "new_name" in raw:
            new_name = _validate_name(raw.get("new_name"))
            if op == "rename" and new_name == current.name:
                unchanged += 1
                continue
            if not current.is_dir:
                old_suffix = Path(current.name.replace("。", ".").replace("．", ".")).suffix.casefold()
                new_suffix = Path(new_name.replace("。", ".").replace("．", ".")).suffix.casefold()
                if old_suffix != new_suffix:
                    raise GuangYaFSChangeError("文件改名不能改变扩展名")
            if op != "copy" and _name_conflict(cache[parent_id], new_name, exclude_id=current.file_id):
                if op == "rename":
                    raise GuangYaFSChangeError("改名目标已被同目录对象占用")
                # 源目录的同名项不必改动；目标原名也空闲时可以先搬走再改名。
                base["move_first"] = True
            base["new_name"] = new_name
        if op in {"move", "relocate", "copy"}:
            target_path = raw["target_path"]
            pending_target = pending_targets.get(target_path)
            if pending_target is None:
                target_id, target_snapshot = resolve_directory(target_path)
                if "target_id" in raw and raw["target_id"] != target_id:
                    raise GuangYaFSChangeStale("已读取的目标目录身份已变化，不能以同名目录替代")
                if target_id == parent_id:
                    if op == "copy":
                        raise GuangYaFSChangeError("复制目标与当前目录相同")
                    if op == "move" or base.get("new_name") == current.name:
                        unchanged += 1
                        continue
                    # 已在目标目录的 relocate 只需改名，不能再次提交原地移动。
                    base["op"] = "rename"
            else:
                if "target_id" in raw:
                    raise GuangYaFSChangeStale("所选目标目录已不再是原来的真实对象")
                target_id, target_snapshot = "", None
            source_path = str(base["source_path"])
            if current.is_dir and (
                target_path == source_path or target_path.startswith(source_path + "/")
            ):
                raise GuangYaFSChangeError(
                    "不能把目录复制到自身或其子目录"
                    if op == "copy"
                    else "不能把目录移动到自身或其子目录"
                )
            target_name = str(base.get("new_name") or current.name)
            if pending_target is None:
                target_items = _list_map(client, target_id, cache)
                if _name_conflict(target_items, target_name) or (
                    (base.get("move_first") or op == "copy") and _name_conflict(target_items, current.name)
                ):
                    raise GuangYaFSChangeError("目标目录中已有同名对象")
            base.update(
                target_path=target_path,
                target_id=target_id,
                target_snapshot=target_snapshot,
            )
            if pending_target is not None:
                base["target_create_path"] = target_path
        if op == "copy" and current.is_dir:
            base["copy_snapshot"] = _copy_tree_snapshot(client, current.file_id)
        if handle in seen_objects:
            raise GuangYaFSChangeError("计划不能重复操作同一个对象")
        seen_objects.add(handle)
        frozen.append(base)

    structural_moves = [
        item
        for item in frozen
        if item.get("op") in {"move", "relocate", "rename", "trash"}
        and bool((item.get("source") or {}).get("is_dir"))
    ]
    # 保留签名计划已有的 rename_dependencies 字段，统一表示前置对象操作，
    # 不引入新旧两套依赖读取路径；旧计划仍按原有子文件改名依赖执行。
    by_id = {str(item["source"]["file_id"]): item for item in frozen if "source" in item}
    if len(by_id) != len(seen_objects):
        raise GuangYaFSChangeError("计划不能通过不同引用重复操作同一个对象")
    for item in frozen:
        source_path = str(item.get("source_path") or "")
        target = str(item.get("target_path") or item.get("parent_path") or "")
        target_id = str(item.get("target_id") or item.get("parent_id") or "")
        for parent in structural_moves:
            parent_path = str(parent["source_path"])
            if item is not parent and (
                source_path.startswith(parent_path + "/") or target == parent_path or target.startswith(parent_path + "/")
                or target_id == parent["source"]["file_id"]
            ):
                if parent["op"] == "trash":
                    target = str(item.get("target_path") or "")
                    moves_out = item["op"] in {"move", "relocate"} and (not (
                        target == parent_path or target.startswith(parent_path + "/")
                    ))
                    trashes_explicit = item["op"] == "trash"
                    if not moves_out and not trashes_explicit:
                        raise GuangYaFSChangeError("清理父目录前必须先移出内容；不能混合改名、复制或回收非空内容")
                    parent["require_empty"] = True
                    if item["op"] == "trash" and item["source"]["is_dir"]:
                        item["require_empty"] = True
                elif parent["op"] != "rename" and item["op"] == "trash" and item["source"]["is_dir"]:
                    item["require_empty"] = True
                parent.setdefault("rename_dependencies", []).append(str((item.get("source") or {}).get("file_id") or item["created_path"]))

    for item in frozen:
        if not item.get("require_empty"):
            continue
        path = str(item["source_path"])
        children = _list_map(client, str(item["source"]["file_id"]), cache)
        for file_id in children:
            child = by_id.get(file_id, {})
            target = str(child.get("target_path") or "")
            escapes = child.get("op") in {"move", "relocate"} and not (
                target == path or target.startswith(path + "/")
            )
            recycled = child.get("op") == "trash" and (
                not child["source"]["is_dir"] or child.get("require_empty")
            )
            if not escapes and not recycled:
                raise GuangYaFSChangeError("父目录仍有未安排移出的内容，不能作为空目录清理")
        # 不能一边向目录放入文件、一边把它当成即将变空的目录回收。
        if any(str(other.get("target_path") or "") == path or
               str(other.get("target_path") or "").startswith(path + "/") for other in frozen):
            raise GuangYaFSChangeError("计划不能把对象移入或复制到待清理目录")

    # 独立操作维持原顺序；依赖项只在全部前置对象完成后排列，支持多层目录。
    pending = sorted(frozen, key=lambda item: bool(item.get("rename_dependencies")))
    frozen = []
    ordered_ids: set[str] = set()
    while pending:
        item = next((item for item in pending if set(item.get("rename_dependencies") or ()).issubset(ordered_ids)), None)
        if item is None:
            raise GuangYaFSChangeError("光鸭变更计划存在无法满足的操作依赖")
        pending.remove(item)
        frozen.append(item)
        ordered_ids.add(str((item.get("source") or {}).get("file_id") or item["created_path"]))

    _validate_plan_names(frozen)
    counts = {
        key: 0
        for key in (
            "rename",
            "move",
            "relocate",
            "copy",
            "trash",
            "create_directory",
        )
    }
    samples: list[str] = []
    for item in frozen:
        op = str(item["op"])
        counts[op] += 1
        if len(samples) >= 6:
            continue
        samples.append(_operation_label(item))

    stats = {"total": len(frozen), "unchanged": unchanged, **counts}
    if not frozen:
        no_changes = {"status": "no_changes", "operations": [], "stats": stats,
                      "samples": [], "trigger_strm": False}
        if batch_summary:
            no_changes["batch_summary"] = batch_summary
        return no_changes
    current = time.time()
    plan = {
        "version": _PLAN_VERSION,
        "plan_id": uuid.uuid4().hex,
        "owner_digest": _owner_digest(owner),
        "credential_generation": client.credential_generation,
        "created_at": _now_iso(),
        "created_at_epoch": current,
        "expires_at_epoch": current + _PLAN_TTL_SECONDS,
        "trigger_strm": trigger_strm,
        "status": "previewed",
        "operations": frozen,
        "stats": stats,
        "samples": samples,
        "execution": {},
    }
    if batch_summary:
        plan["batch_summary"] = batch_summary
    plan["fingerprint"] = _fingerprint(plan)
    with _plan_state_lock():
        _atomic_write(plan)
        maintained = _maintain_fs_change_plans_unlocked(
            preserve_plan_id=plan["plan_id"]
        )
        if (maintained.get("capacity_exceeded") or 0) > 0:
            _discard_fs_change_plan_unlocked(plan["plan_id"])
            raise GuangYaFSChangeError("光鸭变更计划容量已满，请稍后重试")
    return plan


def _copy_tree_snapshot(client, root_id, cancel_check=None):
    """完整遍历形成有界摘要：源身份/版本与复制内容分开，不把目录外壳当成功。"""
    pending: list[tuple[str, tuple[str, ...]]] = [(str(root_id), ())]
    seen, paths, rows, directories = {str(root_id)}, set(), [], 0
    while pending:
        directory_id, path = pending.pop()
        directories += 1
        if directories > _MAX_COPY_DIRS:
            raise GuangYaFSChangeError("复制目录超过完整核验预算，请拆分子目录处理")
        if cancel_check is not None:
            cancel_check()
        for entry in client.list_dir(directory_id):
            relative = (*path, entry.name)
            if entry.file_id in seen or relative in paths or str(entry.parent_id or "0") != directory_id:
                raise GuangYaFSChangeStale("复制目录清单不一致，请重新读取")
            seen.add(entry.file_id)
            paths.add(relative)
            rows.append((relative, _snapshot(entry)))
            if len(rows) > _MAX_COPY_ITEMS:
                raise GuangYaFSChangeError("复制目录超过完整核验预算，请拆分子目录处理")
            if entry.is_dir:
                pending.append((entry.file_id, relative))
    rows.sort(key=lambda row: row[0])
    def digest(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    content = [(path, row["is_dir"], 0 if row["is_dir"] else row["size"],
                "" if row["is_dir"] else row["etag"]) for path, row in rows]
    return {"identity": digest(rows), "content": digest(content), "items": len(rows)}


def _guard_tree_matches(client: GuangYaClient, root: dict, expected: list, cancel_check=None) -> bool:
    """比较完整子树身份，不能只凭目录外壳决定残留隔离。"""
    if len(expected) > _MAX_COPY_ITEMS or any(not isinstance(item, dict) for item in expected):
        raise GuangYaFSChangeError("目录守卫清单无效或超出完整核验预算")
    rows, queue, seen = [root], [root["file_id"]], {root["file_id"]}
    directories = 0
    while queue:
        identifier = queue.pop()
        directories += 1
        if directories > _MAX_COPY_DIRS:
            raise GuangYaFSChangeError("目录守卫超过完整核验预算")
        if cancel_check is not None:
            cancel_check()
        for item in client.list_dir(identifier):
            if item.file_id in seen or item.parent_id != identifier:
                raise GuangYaFSChangeStale("目录清单父子关系或身份发生变化")
            seen.add(item.file_id)
            rows.append(_snapshot(item))
            if len(rows) > _MAX_COPY_ITEMS:
                raise GuangYaFSChangeError("目录守卫超过完整核验预算")
            if item.is_dir:
                queue.append(item.file_id)
    return sorted(rows, key=lambda item: item["file_id"]) == sorted(expected, key=lambda item: item.get("file_id", ""))


def _find_current(
    client: GuangYaClient, snapshot: dict[str, Any], *,
    cache: dict[str, GuangYaFile | None] | None = None,
    directory_cache: dict[str, dict[str, GuangYaFile]] | None = None,
) -> GuangYaFile | None:
    cache = cache if cache is not None else {}
    file_id = str(snapshot.get("file_id") or "")
    if file_id in cache:
        return cache[file_id]
    try:
        # 明确的当前详情（包括对象不存在）不能被较旧的列表翻案。
        current = client.file_info(file_id)
    except Exception:  # noqa: BLE001 - Provider/SDK 可抛出多种传输异常
        # 只有详情读取失败才使用父目录列表回退；同次核对可复用冲突清单。
        try:
            current = _list_map(client, str(snapshot.get("parent_id") or "0"),
                                directory_cache if directory_cache is not None else {}).get(file_id)
        except Exception:  # noqa: BLE001 - 只读回退失败按对象不存在处理
            current = None
    cache[file_id] = current
    return current


def _verify_directory_snapshot(
    client: GuangYaClient, directory_id: str, snapshot: dict[str, Any] | None, *,
    cache: dict[str, GuangYaFile | None] | None = None,
    directory_cache: dict[str, dict[str, GuangYaFile]] | None = None,
) -> bool:
    if directory_id == "0":
        return True
    if not isinstance(snapshot, dict):
        return False
    current = _find_current(client, snapshot, cache=cache, directory_cache=directory_cache)
    # 目录内容变化可能更新 etag/utime；目标目录只冻结身份、名称与位置，
    # 同名占用会在每一项写入前另行完整读取。
    return bool(
        current
        and current.is_dir
        and current.file_id == str(snapshot.get("file_id") or "")
        and current.parent_id == str(snapshot.get("parent_id") or "0")
        and current.name == str(snapshot.get("name") or "")
    )


def _directory_id(
    item: dict[str, Any],
    created_targets: dict[str, dict[str, Any]] | None,
    *,
    allow_pending: bool = False,
    role: str = "target",
) -> str:
    create_path = str(item.get(f"{role}_create_path") or "")
    if not create_path:
        return str(item.get(f"{role}_id") or "0")
    created_id = str(((created_targets or {}).get(create_path) or {}).get("file_id") or "")
    if created_id:
        return created_id
    if allow_pending:
        return ""
    raise GuangYaFSChangeStale("计划中的新建目标目录尚未就绪")


def _stage_identity_matches(item, snapshot, created_targets):
    source = item.get("source") or {}
    if item["op"] != "copy":
        return snapshot.get("file_id") == source.get("file_id")
    return (len(_operation_actions(item)) == 2 and bool(snapshot.get("file_id"))
            and snapshot["file_id"] != source.get("file_id")
            and snapshot.get("parent_id") == _directory_id(item, created_targets)
            and snapshot.get("name") == source.get("name")
            and snapshot.get("is_dir") == source.get("is_dir"))


def _frozen_path_matches(client, snapshot, path, cache, cancel_check):
    """沿真实 ID 的有界父链核对冻结路径；不遍历子树或持久化第二套快照。"""
    file_id = str(snapshot.get("file_id") or "")
    if file_id == "0" and snapshot.get("is_dir"):
        return path == "/"
    _find_current(client, snapshot, cache=cache)
    entry = locate_file_in_roots(
        client, snapshot, [{"file_id": "0", "path": "/", "is_dir": True}],
        cache=cache, check_cancelled=cancel_check or (lambda: None),
    )
    if entry is None:
        return False
    current_path = _full_path(entry["parent_path"], entry["name"])
    if current_path == path:
        return True
    # 原路径解析允许唯一大小写别名；不能将共存的 D/d 当成同一个目录。
    if current_path.casefold() == path.casefold():
        try:
            return resolve_workspace_path(client, path).file_id == file_id
        except GuangYaWorkspaceError:
            pass
    return False


def _preflight_operation(
    client: GuangYaClient,
    item: dict[str, Any],
    *,
    created_targets: dict[str, dict[str, Any]] | None = None,
    allow_pending_target: bool = False,
    completed_objects: set[str] | None = None,
    source_snapshot: dict[str, Any] | None = None,
    cancel_check: Callable[[], None] | None = None,
    path_cache: dict[str, GuangYaFile | None] | None = None,
    directory_cache: dict[str, dict[str, GuangYaFile]] | None = None,
) -> None:
    op = str(item.get("op") or "")
    # 整单只读预检可共享；每项真正写前、写后阶段和轮询均另用新缓存。
    path_cache = path_cache if path_cache is not None else {}
    directory_cache = directory_cache if directory_cache is not None else {}
    # 新建目录没有预先存在的快照：沿本次已核验创建链检查名称和位置，
    # 防止外部搬走父目录后，后续操作仍按旧 ID 写入错误的归档路径。
    dependency = str(item.get("parent_create_path" if op == "create_directory" else "target_create_path") or "")
    while dependency and created_targets is not None:
        directory = created_targets.get(dependency) or {}
        if not _verify_directory_snapshot(client, str(directory.get("file_id") or ""), directory,
                                          cache=path_cache, directory_cache=directory_cache):
            raise GuangYaFSChangeStale("计划中新建目录尚未就绪或位置已变化，请重新预览")
        dependency = str(directory.get("parent_create_path") or "")
    if op == "create_directory":
        parent_id = _directory_id(item, created_targets, role="parent", allow_pending=allow_pending_target)
        if not parent_id:
            return
        if (not item.get("parent_create_path") and not _verify_directory_snapshot(
                client, parent_id, item.get("parent_snapshot"),
                cache=path_cache, directory_cache=directory_cache)
                or not _frozen_path_matches(client, {"file_id": parent_id, "is_dir": True},
                                            item["parent_path"], path_cache, cancel_check)):
            raise GuangYaFSChangeStale("新建目录的父目录已变化，请重新预览")
        siblings = _list_map(client, parent_id, directory_cache)
        if _name_conflict(siblings, str(item.get("name") or "")):
            raise GuangYaFSChangeStale("新建目录名称已被占用，请重新预览")
        return
    source = source_snapshot if source_snapshot is not None else item.get("source")
    if source_snapshot is not None and not _stage_identity_matches(item, source_snapshot, created_targets):
        raise GuangYaFSChangeStale("中间态对象身份与冻结计划不一致")
    dependencies = set(item.get("rename_dependencies") or ())
    if dependencies and completed_objects is not None and not dependencies.issubset(completed_objects):
        raise GuangYaFSChangeStale("前置子项变更未全部成功，未执行父目录操作")
    if dependencies and completed_objects is not None and source_snapshot is None:
        # 初次执行允许已批准子项更新目录版本；半步恢复必须符合后置快照。
        source_matches = _verify_directory_snapshot(client, str((source or {}).get("file_id") or ""), source,
                                                    cache=path_cache, directory_cache=directory_cache)
    else:
        source_matches = isinstance(source, dict) and _snapshot_matches(
            _find_current(client, source, cache=path_cache, directory_cache=directory_cache), source,
            exact_updated_at=source_snapshot is not None)
    if not source_matches:
        raise GuangYaFSChangeStale("光鸭对象已变化，请重新预览")
    assert isinstance(source, dict)
    if "expected_tree" in item and not _guard_tree_matches(client, source, item["expected_tree"], cancel_check):
        raise GuangYaFSChangeStale("残留目录内容已变化，保留原目录并停止后续写入")
    parent_path = item["source_parent_path"]
    if source_snapshot is not None and source["parent_id"] != item["source"]["parent_id"]:
        parent_path = item["target_path"]  # 已核验的移动/复制半步沿原目标路径继续。
    if not _frozen_path_matches(client, source, _full_path(parent_path, source["name"]), path_cache, cancel_check):
        raise GuangYaFSChangeStale("光鸭对象父路径已变化，请重新预览")
    if op == "copy" and source_snapshot is not None:
        if _verified_copy(client, item, created_targets, "", cancel_check,
                          name=source_snapshot["name"], copied_id=source_snapshot["file_id"],
                          cache=path_cache, directory_cache=directory_cache) is None:
            raise GuangYaFSChangeStale("复制源或副本内容已变化，请重新预览")
    elif op == "copy" and source["is_dir"]:
        if not isinstance(item.get("copy_snapshot"), dict):
            raise GuangYaFSChangeStale("复制计划缺少完整目录快照，请重新预览")
        if _copy_tree_snapshot(client, source["file_id"], cancel_check) != item["copy_snapshot"]:
            raise GuangYaFSChangeStale("复制源目录内容已变化，请重新预览")
    if item.get("require_empty") and (completed_objects is not None or not dependencies):
        if _list_map(client, str(source["file_id"]), directory_cache):
            raise GuangYaFSChangeStale("目录仍包含内容，未执行空目录清理")
    if op in {"rename", "relocate"} and not item.get("move_first"):
        siblings = _list_map(client, str(source.get("parent_id") or "0"), directory_cache)
        if _name_conflict(
            siblings,
            str(item.get("new_name") or ""),
            exclude_id=str(source.get("file_id") or ""),
        ):
            raise GuangYaFSChangeStale("改名目标已被占用，请重新预览")
    if op in {"move", "relocate", "copy"}:
        target_id = _directory_id(
            item,
            created_targets,
            allow_pending=allow_pending_target,
        )
        if not target_id:
            return
        if (not item.get("target_create_path") and not _verify_directory_snapshot(
                client, target_id, item.get("target_snapshot"),
                cache=path_cache, directory_cache=directory_cache)
                or not _frozen_path_matches(client, {"file_id": target_id, "is_dir": True},
                                            item["target_path"], path_cache, cancel_check)):
            raise GuangYaFSChangeStale("目标目录已变化，请重新预览")
        siblings = _list_map(client, target_id, directory_cache)
        target_name = str(item.get("new_name") or source.get("name") or "")
        exclude_id = str(source.get("file_id") or "") if op != "copy" or source_snapshot else ""
        if _name_conflict(siblings, target_name, exclude_id=exclude_id) or (
            (item.get("move_first") or op == "copy" and source_snapshot is None)
            and _name_conflict(siblings, str(source.get("name") or ""), exclude_id=exclude_id)
        ):
            raise GuangYaFSChangeStale("目标目录中已有同名对象，请重新预览")


def _written_source(client, item, created_targets, directory_cache=None):
    source = item.get("source") or {}
    rename = item["op"] == "rename"
    parent_id = str(source.get("parent_id") or "0") if rename else _directory_id(item, created_targets)
    name = str(item["new_name"] if item["op"] in {"rename", "relocate"} else source.get("name") or "")
    return next((row for row in _list_map(client, parent_id, directory_cache if directory_cache is not None else {}).values()
                 if row.file_id == str(source.get("file_id") or "") and row.name == name), None)


def _verify_after(
    client: GuangYaClient,
    item: dict[str, Any],
    created_id: str = "",
    *,
    created_targets: dict[str, dict[str, Any]] | None = None,
    copy_task_id: str = "",
    cancel_check: Callable[[], None] | None = None,
    stage_source: dict[str, Any] | None = None,
    directory_cache: dict[str, dict[str, GuangYaFile]] | None = None,
) -> bool:
    op = str(item.get("op") or "")
    directory_cache = directory_cache if directory_cache is not None else {}
    if op == "create_directory":
        parent_id = _directory_id(item, created_targets, role="parent")
        matches = [
            row
            for row in _list_map(client, parent_id, directory_cache).values()
            if row.is_dir and row.name == str(item.get("name") or "")
        ]
        return len(matches) == 1 and (
            not created_id or matches[0].file_id == created_id
        )
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    file_id = str(source.get("file_id") or "")
    if op == "trash":
        return all(
            row.file_id != file_id
            for row in _list_map(client, str(source.get("parent_id") or "0"), directory_cache).values()
        )
    if op in {"rename", "move", "relocate"}:
        return _written_source(client, item, created_targets, directory_cache) is not None
    if op == "copy":
        if len(_operation_actions(item)) == 2 and not stage_source:
            return False
        return _verified_copy(client, item, created_targets, copy_task_id, cancel_check,
                              name=str(item.get("new_name") or source.get("name") or ""),
                              copied_id=str((stage_source or {}).get("file_id") or "")) is not None
    return False


def _verified_copy(client, item, created_targets, task_id, cancel_check, *, name, copied_id="",
                   cache=None, directory_cache=None):
    """Provider终态和完整副本共同核验，返回副本身份供同计划的后续改名使用。"""
    if task_id:
        state = guangya_provider_task_state(client.task_status(task_id))
        if state == "failed":
            raise GuangYaWriteRejected("copy", code="task_failed")
        if state == "running":
            raise GuangYaFSCopyPending()
        if state != "completed":
            return None
    source = item["source"]
    if not _snapshot_matches(_find_current(client, source, cache=cache, directory_cache=directory_cache), source):
        return None
    copied = _copy_output(client, item, created_targets, cancel_check, name=name, copied_id=copied_id,
                          directory_cache=directory_cache)
    if copied is not None and source["is_dir"] and _copy_tree_snapshot(client, source["file_id"], cancel_check) != item["copy_snapshot"]:
        return None
    return copied


def _copy_output(client, item, created_targets, cancel_check, *, name, copied_id="", directory_cache=None):
    """按冻结内容核对副本；已完成复制的源可以被同一计划后续合法修改。"""
    source = item["source"]
    target_id = _directory_id(item, created_targets)
    rows = client.list_dir(target_id)
    if directory_cache is not None:
        directory_cache[target_id] = {row.file_id: row for row in rows}
    matches = [row for row in rows
               if row.name == name and row.is_dir == bool(source["is_dir"])]
    if len(matches) != 1 or matches[0].file_id == source["file_id"]:
        return None
    copied = matches[0]
    if copied_id and str(copied.file_id) != copied_id:
        return None
    if copied.is_dir:
        expected = item.get("copy_snapshot")
        valid = (isinstance(expected, dict)
                 and _copy_tree_snapshot(client, copied.file_id, cancel_check)["content"] == expected["content"])
    else:
        size = max(0, int(source.get("size") or 0))
        valid = ((not size or int(copied.size or 0) == size)
                 and (not source.get("etag") or str(copied.etag or "") in {"", str(source["etag"])}))
    return copied if valid else None


def update_fs_change_plan_execution(
    plan_id: str,
    *,
    status: str,
    execution: dict[str, Any],
    expected_statuses: set[str] | frozenset[str] | None = None,
    expected_job_id: str = "",
    expected_lease_generation: int | None = None,
) -> dict[str, Any]:
    """在跨进程锁内执行带前置状态/job_id/执行代数的文件级 CAS。"""
    safe_status = (status or "").strip().casefold()
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
        safe_job_id = (expected_job_id or "").strip().casefold()
        if safe_job_id and (
            not _SAFE_JOB_ID.fullmatch(safe_job_id)
            or not hmac.compare_digest(str(payload.get("job_id") or ""), safe_job_id)
        ):
            raise GuangYaFSChangeStale("光鸭变更计划任务绑定已变化")
        if expected_lease_generation is not None and (payload.get("execution") or {}).get("lease_generation") != expected_lease_generation:
            raise GuangYaFSChangeStale("光鸭变更执行代数已变化，请勿重复执行")
        payload["status"] = safe_status
        payload["execution"] = dict(execution)
        payload["updated_at"] = _now_iso()
        _atomic_write(payload)
        return payload


def _operation_stat_key(operation: str) -> str:
    try:
        return {
            "rename": "renamed",
            "move": "moved",
            "relocate": "relocated",
            "copy": "copied",
            "trash": "trashed",
            "create_directory": "created",
        }[operation]
    except KeyError as exc:
        raise GuangYaFSChangeError("光鸭变更计划包含未知操作") from exc


def _operation_actions(item: dict[str, Any]) -> tuple[str, ...]:
    op = str(item["op"])
    if op == "copy" and item.get("new_name") not in (None, item["source"]["name"]):
        return ("copy", "rename")
    if op != "relocate":
        return (op,)
    if item["new_name"] == item["source"]["name"]:
        return ("move",)
    return ("move", "rename") if item.get("move_first") else ("rename", "move")


def _verify_fs_stage(client, item, action, created_id, created_targets, cancel_check, copy_task_id, stage_source=None):
    """复用同一次写后读取：返回阶段核验与可用于续行的中间版本，不额外查询。"""
    intermediate = action != _operation_actions(item)[-1]
    observed = None
    copy_pending = False

    def verify():
        nonlocal observed, copy_pending
        copy_pending = False
        try:
            if intermediate:
                observed = (_verified_copy(client, item, created_targets, copy_task_id, cancel_check,
                                           name=item["source"]["name"]) if action == "copy" else
                            _written_source(client, {**item, "op": action}, created_targets))
                return observed is not None
            return _verify_after(client, item, created_id, created_targets=created_targets,
                                 copy_task_id=copy_task_id, cancel_check=cancel_check, stage_source=stage_source)
        except GuangYaFSCopyPending:
            copy_pending = True
            return False

    attempts = _COPY_FOREGROUND_ATTEMPTS if copy_task_id else 21
    verified = verify_guangya_write(verify, cancel_check=cancel_check, attempts=attempts)
    if not verified and copy_pending:
        raise GuangYaFSCopyPending()
    if verified and observed is not None:
        if action == "copy":
            return True, _snapshot(observed)
        source = item["source"]
        expected = {**source, "name": item["new_name"] if action == "rename" else source["name"],
                    "parent_id": source["parent_id"] if action == "rename" else _directory_id(item, created_targets),
                    "updated_at": observed.updated_at}
        if _snapshot_matches(observed, expected):
            return True, _snapshot(observed)
    return verified, None


def _apply_fs_operation(
    client: GuangYaClient, item: dict[str, Any], outcome: dict[str, Any],
    created_targets: dict[str, dict[str, Any]], cancel_check: Callable[[], None] | None,
    checkpoint: Callable[[], None], stage_sources: dict[str, dict[str, Any]], copy_tasks: dict[str, str],
) -> str:
    source = item.get("source") or {}
    created_id = ""
    actions = _operation_actions(item)
    try:
        for action in actions[len(outcome["completed_actions"]):]:
            stage_source = stage_sources.get(str(outcome["position"]))
            if stage_source is not None:
                _preflight_operation(client, item, created_targets=created_targets,
                                     source_snapshot=stage_source, cancel_check=cancel_check)
            if item["op"] == "copy" and action == "rename":
                if stage_source is None:
                    raise GuangYaFSChangeStale("副本身份尚未核验，不能执行改名")
                source = stage_source
            if cancel_check is not None:
                cancel_check()
            before_write = dict(outcome)
            outcome.update(status="unknown", reason="write_outcome_unknown")
            try:
                checkpoint()  # 先落盘意图，崩溃后不得把已发起当作未开始。
                if cancel_check is not None:
                    cancel_check()  # fsync期间到达的取消也必须在发请求前生效。
            except Exception:
                outcome.clear()
                outcome.update(before_write)  # 日志未能保存时本阶段尚未发起。
                raise
            if action == "rename":
                client.rename(str(source["file_id"]), str(item["new_name"]))
            elif action == "move":
                client.move([str(source["file_id"])], _directory_id(item, created_targets))
            elif action == "copy":
                task_id = client.copy([str(source["file_id"])], _directory_id(item, created_targets))
                if task_id:
                    copy_tasks[str(outcome["position"])] = task_id
                    checkpoint()  # 保留已受理任务；中断恢复仅读查询，不再次复制。

            elif action == "create_directory":
                created_id = client.create_dir(str(item["name"]), _directory_id(item, created_targets, role="parent"))
                created_targets[str(item["created_path"])] = {
                    "file_id": created_id, "name": str(item["name"]),
                    "parent_id": _directory_id(item, created_targets, role="parent"),
                    "parent_create_path": str(item.get("parent_create_path") or ""),
                }
            elif action == "trash":
                delete_operation = None
                if item.get("require_empty"):
                    current = client.file_info(str(source["file_id"]))
                    if current is None:
                        raise GuangYaFSChangeStale("待清理目录已变化，请重新核对")
                    delete_operation = lambda: client.delete_empty_directory(
                        str(source["file_id"]), expected_etag=(current.etag or ""),
                        expected_updated_at=max(0, (current.updated_at or 0)),
                    )
                execute_recycle_bin_delete(
                    client, trigger="agent_guangya_fs_change", reason="Agent 已确认的光鸭文件变更计划",
                    candidate=DeleteCandidate(file_id=str(source["file_id"]), name=str(source.get("name") or ""),
                        parent_id=str(source.get("parent_id") or "0"), size=max(0, int(source.get("size") or 0)),
                        gcid=str(source.get("etag") or "")),
                    safe_failure_message="光鸭对象移入回收站失败", delete_operation=delete_operation,
                )
            else:
                raise GuangYaFSChangeError("光鸭变更计划包含未知操作")
            # 写入已返回；trash 的成功审计也已落盘。后续失败属于回读，不是审计丢失。
            outcome["reason"] = "verification_pending"
            verified, stage_source = _verify_fs_stage(client, item, action, created_id, created_targets, cancel_check,
                copy_tasks.get(str(outcome["position"]), "") if not stage_source else "", stage_source)
            if not verified:
                raise GuangYaFSChangeError("写入后的云端状态尚未核验，未执行后续操作")
            if stage_source is not None:
                stage_sources[str(outcome["position"])] = stage_source
            outcome["completed_actions"].append(action)
            outcome["status"] = "completed" if action == actions[-1] else "partial"
            outcome.pop("reason", None)
            checkpoint()
    except Exception as exc:
        # 保留已经取得的创建身份，避免回读失败后按同名目录误判结果。
        setattr(exc, "fs_created_id", created_id)
        raise
    return created_id


def _preflight_fs_change_plan(client, operations, outcomes, cancel_check):
    """首次执行需整单通过写前核对；中断恢复则按已记录的阶段逐项核对。"""
    path_cache = {}
    directory_cache = {}
    for item, outcome in zip(operations, outcomes):
        if cancel_check is not None:
            cancel_check()
        try:
            _preflight_operation(client, item, allow_pending_target=True, cancel_check=cancel_check,
                                 path_cache=path_cache, directory_cache=directory_cache)
        except GuangYaFSChangeStale:
            outcome.update(status="blocked", reason="precondition_failed")
            raise


def _can_continue_operation(client, item, outcome, source_snapshot, created_targets, completed_objects):
    actions = _operation_actions(item)
    if not (len(actions) == 2
            and outcome["completed_actions"] == [actions[0]] and not outcome.get("reason")
            and isinstance(source_snapshot, dict)):
        return False
    try:
        _preflight_operation(client, item, source_snapshot=source_snapshot,
                             created_targets=created_targets, completed_objects=completed_objects)
        return True
    except GuangYaFSChangeStale:
        outcome["reason"] = "precondition_failed"
    except Exception:
        outcome["reason"] = "verification_pending"
    return False


def _reconcile_fs_operations(client, operations, outcomes, created_targets, stats, cancel_check, stage_sources, copy_tasks):
    """仅读核对事实；仅有签名中间快照的已核验半步可继续，未知动作不重放。"""
    resumable, completed_objects, waiting_copies = set(), set(), set()
    for item, outcome in zip(operations, outcomes):
        if cancel_check is not None:
            cancel_check()
        status = outcome["status"]
        position = str(outcome["position"])
        stage_source = stage_sources.get(position)
        if status == "partial" and _can_continue_operation(client, item, outcome,
                stage_source, created_targets, completed_objects):
            resumable.add(outcome["position"])
        if status not in {"completed", "unknown"}:
            continue
        created_id = str((created_targets.get(item.get("created_path")) or {}).get("file_id") or "")
        verifiable = status == "completed" or item["op"] in {"rename", "move", "relocate"}
        # 新建/复制的同名对象不是身份凭据；原位置消失也不证明已进回收站。
        verifiable &= item["op"] != "create_directory" or bool(created_id)
        copy_task_id = copy_tasks.get(position, "")
        if item["op"] == "copy":
            # 旧同步回执可核验；未知复制只有已落盘的任务编号才能追踪，不能凭同名副本补认成功。
            verifiable = bool(copy_task_id) or status == "completed"
        try:
            verified = verifiable and _verify_after(client, item, created_id, created_targets=created_targets,
                                                   copy_task_id=copy_task_id if status != "completed" and not stage_source else "",
                                                   cancel_check=cancel_check,
                                                   stage_source=stage_source)
            if (not verified and item["op"] == "copy" and len(_operation_actions(item)) == 2
                    and not stage_source and copy_task_id):
                copied = _verified_copy(client, item, created_targets, copy_task_id, cancel_check,
                                        name=item["source"]["name"])
                if copied is not None:
                    snapshot = stage_sources[position] = _snapshot(copied)
                    outcome.update(status="partial", completed_actions=["copy"])
                    outcome.pop("reason", None)
                    if _can_continue_operation(client, item, outcome, snapshot, created_targets, completed_objects):
                        resumable.add(outcome["position"])
                    continue
        except GuangYaFSCopyPending:
            waiting_copies.add(outcome["position"])
            verified = False
        except GuangYaWriteRejected:
            outcome.update(status="failed", reason="write_rejected")
            continue
        except Exception:  # 读失败只能保留未知，不能落为可重试。
            verified = False
        if verified:
            outcome.update(status="completed", completed_actions=list(_operation_actions(item)))
            outcome.pop("reason", None)
            completed_objects.add(str((item.get("source") or {}).get("file_id") or item["created_path"]))
        else:
            outcome.update(status="unknown", reason="verification_pending")
            actions = _operation_actions(item)
            if item["op"] == "relocate" and len(actions) == 2:
                try:
                    if _verify_after(client, {**item, "op": actions[0]}, created_targets=created_targets):
                        outcome.update(status="partial", completed_actions=[actions[0]])
                except Exception:
                    pass  # 中间状态也不可读时仍是未知，不补发任一阶段。
    for key in ("renamed", "moved", "relocated", "copied", "trashed", "created", "failed"):
        stats[key] = 0
    for item, outcome in zip(operations, outcomes):
        if outcome["status"] == "completed":
            stats[_operation_stat_key(item["op"])] += 1
        elif outcome["status"] != "not_started" and outcome["position"] not in resumable | waiting_copies:
            stats["failed"] += 1
    uncertain = bool(stats.get("audit_failures") or any(
        row["status"] in {"unknown", "partial"} and row["position"] not in resumable | waiting_copies for row in outcomes))
    return uncertain, resumable, waiting_copies


def _reset_fs_checkpoint_cursor(plan_id, execution):
    path = _journal_path(plan_id)
    execution.update(checkpoint_id=uuid.uuid4().hex, journal_sequence=0,
                     journal_offset=path.stat().st_size if path.exists() else 0)


def _read_fs_checkpoints(plan):
    """只重放签名的事实增量，不重放操作。历史普通审计行保持原格式。"""
    base = plan["execution"]
    execution: dict[str, Any] = {**base, "operation_items": [dict(row) for row in base["operation_items"]],
                 "stats": dict(base["stats"]), "created_targets": dict(base.get("created_targets") or {}),
                 "stage_sources": dict(base.get("stage_sources") or {}),
                 "copy_tasks": dict(base.get("copy_tasks") or {})}
    path = _journal_path(plan["plan_id"])
    try:
        offset = int(base["journal_offset"])
        if path.is_symlink() or not 0 <= offset <= path.stat().st_size:
            raise ValueError("checkpoint offset")
        with path.open("rb") as stream:
            stream.seek(offset)
            tail = stream.read(16 * _MAX_PLAN_BYTES + 1)
        if len(tail) > 16 * _MAX_PLAN_BYTES or tail and not tail.endswith(b"\n"):
            raise ValueError("checkpoint journal incomplete")
        if not tail:
            # 没有阶段记录时无法区分尚未发起与日志整段丢失，不能据此追加写入。
            raise ValueError("checkpoint journal has no durable stages")
        for line in tail.splitlines():
            event = json.loads(line)
            if event.get("action") != "checkpoint":
                continue
            if (not hmac.compare_digest(str(event.get("auth") or ""), _auth(event))
                    or event.get("plan_id") != plan["plan_id"] or event.get("job_id") != plan["job_id"]
                    or event.get("checkpoint_id") != base["checkpoint_id"]
                    or event.get("lease_generation") != base["lease_generation"]
                    or event.get("sequence") != execution["journal_sequence"] + 1):
                raise ValueError("checkpoint identity")
            delta = event["data"]
            if "operation_item" in delta:
                row = delta["operation_item"]
                index = int(row["position"]) - 1
                if not 0 <= index < len(plan["operations"]) or row["operation"] != plan["operations"][index]["op"]:
                    raise ValueError("checkpoint operation")
                execution["operation_items"][index] = row
                if "copy_task_id" in delta:
                    if row["operation"] != "copy" or not isinstance(delta["copy_task_id"], str):
                        raise ValueError("checkpoint copy task")
                    execution["copy_tasks"][str(row["position"])] = delta["copy_task_id"]
                if "stage_source" in delta:
                    snapshot = delta["stage_source"]
                    if not _stage_identity_matches(plan["operations"][index], snapshot, execution["created_targets"]):
                        raise ValueError("checkpoint source identity")
                    execution["stage_sources"][str(row["position"])] = snapshot
            if "created_target" in delta:
                target = dict(delta["created_target"])
                execution["created_targets"][target.pop("path")] = target
            execution.update(stats=delta["stats"], finalizing=delta["finalizing"], journal_sequence=event["sequence"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise GuangYaFSChangeError("中断检查点不可核验，请先核对云端结果") from exc
    return execution


def _write_fs_checkpoint(plan_id, job_id, execution, *, item=None, outcome=None, begin_finalization=False):
    """持有整理全局锁的 worker 追加 O(1) 阶段记录；完整快照仅在准入/终态保存。"""
    execution["finalizing"] |= begin_finalization
    delta = {"stats": execution["stats"], "finalizing": execution["finalizing"]}
    if outcome is not None:
        delta["operation_item"] = outcome
        task_id = (execution.get("copy_tasks") or {}).get(str(outcome["position"]))
        if task_id:
            delta["copy_task_id"] = task_id
        source_snapshot = (execution.get("stage_sources") or {}).get(str(outcome["position"]))
        if source_snapshot is not None:
            delta["stage_source"] = source_snapshot
        assert item is not None
        path = str(item.get("created_path") or "")
        if path in execution["created_targets"]:
            delta["created_target"] = {"path": path, **execution["created_targets"][path]}
    event = {"action": "checkpoint", "at": _now_iso(), "plan_id": plan_id, "job_id": job_id,
             "checkpoint_id": execution["checkpoint_id"], "lease_generation": execution["lease_generation"],
             "sequence": execution["journal_sequence"] + 1, "data": delta}
    event["auth"] = _auth(event)
    try:
        _append_journal(plan_id, event)
        execution["journal_sequence"] = event["sequence"]
    except Exception as exc:
        raise GuangYaFSCheckpointError("执行检查点未能保存，已停止后续变更") from exc


def _finish_fs_change_execution(
    plan_id, job_id, plan, result, *, execution, checkpoint,
    successful_operations, persistence_uncertain, cancel_check,
):
    """正常执行与中断核对共用同一收尾，不重复未知的 STRM 提交。"""
    stats = result["stats"]
    successful = (
        stats["renamed"]
        + stats["moved"]
        + stats["relocated"]
        + stats["copied"]
        + stats["trashed"]
        + stats["created"]
    )
    partial = stats["failed"] > 0 or persistence_uncertain
    if bool(plan.get("trigger_strm")) and successful > 0:
        if execution.get("finalizing"):
            stats["strm_scope_unknown"] = 1
            persistence_uncertain = partial = True
        else:
            try:
                checkpoint(begin_finalization=True)
                if cancel_check is not None:
                    cancel_check()
            except GuangYaFSCheckpointError:
                stats["audit_failures"] += 1
                persistence_uncertain = partial = True
            else:
                stats.update(trigger_cloud_changes(successful_operations, sources=execution.get("strm_scope")))
                partial |= bool(stats.get("strm_trigger_failed"))
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
                **execution,
                "finished_at": finished_at,
                **stats,
            },
            expected_statuses={"running"},
            expected_job_id=job_id,
            expected_lease_generation=execution["lease_generation"],
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
    result.update(partial=partial or persistence_uncertain, requires_manual=persistence_uncertain)
    return result


def _load_fs_execution_plan(payload, lease_generation):
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
        require_confirmed=False,
    )
    recovering = str(plan.get("status") or "") != "queued"
    previous = plan.get("execution") or {}
    if recovering and not (
        lease_generation > int(previous.get("lease_generation") or 0)
        and inspect_fs_change_plan_recovery(payload, job_id=job_id,
            owner_digest=str(payload.get("owner_digest") or ""),
            lease_generation=int(previous.get("lease_generation") or 0))["resumable"]
    ):
        raise GuangYaFSChangeStale("光鸭变更计划没有可核对的中断检查点")
    if not hmac.compare_digest(
        str(plan.get("job_id") or ""), job_id
    ):
        raise GuangYaFSChangeStale("光鸭变更计划任务绑定已变化")
    if not hmac.compare_digest(
        str(plan.get("owner_digest") or ""), str(payload.get("owner_digest") or "")
    ):
        raise GuangYaFSChangeError("光鸭变更任务会话不匹配")
    raw_generation = payload.get("credential_generation")
    expected_generation = int(raw_generation) if raw_generation is not None else -1
    if recovering and plan["status"] == "running":
        plan["execution"] = _read_fs_checkpoints(plan)
    return plan, recovering, expected_generation


def execute_fs_change_plan(
    payload: dict[str, Any],
    *,
    cancel_check: Callable[[], None] | None = None,
    client_factory: Callable[[], GuangYaClient] = GuangYaClient,
    lease_generation: int = 0,
) -> dict[str, Any]:
    plan, recovering, expected_generation = _load_fs_execution_plan(payload, lease_generation)
    plan_id, job_id = str(plan["plan_id"]), str(payload["job_id"])
    previous = plan.get("execution") or {}
    client = client_factory()
    stats = {
        "total": len(plan.get("operations") or []),
        "renamed": 0,
        "moved": 0,
        "relocated": 0,
        "copied": 0,
        "trashed": 0,
        "created": 0,
        "failed": 0,
        "verification_failed": 0,
        "precondition_failed": 0,
        "audit_failures": 0,
    }
    operations = list(plan.get("operations") or [])
    outcomes = sanitize_organize_operation_result({"operation_items": [
        {"position": index, "operation": item["op"], "label": _operation_label(item),
         "status": "not_started", "completed_actions": []}
        for index, item in enumerate(operations, start=1)
    ]})["operation_items"]
    if recovering:
        outcomes = previous["operation_items"]
        stats.update(previous.get("stats") or {})
    result = {"partial": False, "requires_manual": False, "stats": stats, "operation_items": outcomes}
    started_at = str(previous.get("started_at") or _now_iso())
    created_targets = dict(previous.get("created_targets") or {}) if recovering else {}
    strm_scope = previous.get("strm_scope") if recovering else None
    finalizing = bool(previous.get("finalizing")) if recovering else False
    persistence_uncertain = False
    resumable = set()
    stage_sources = dict(previous.get("stage_sources") or {}) if recovering else {}
    copy_tasks = dict(previous.get("copy_tasks") or {}) if recovering else {}

    execution: dict[str, Any] = {"checkpoint_version": 2, "lease_generation": lease_generation,
                 "started_at": started_at, "stats": stats, "operation_items": outcomes,
                 "created_targets": created_targets, "stage_sources": stage_sources, "copy_tasks": copy_tasks,
                 "strm_scope": strm_scope, "finalizing": finalizing}

    def checkpoint(*, begin_finalization=False):
        _write_fs_checkpoint(plan_id, job_id, execution,
            item=None if begin_finalization else item, outcome=None if begin_finalization else outcome,
            begin_finalization=begin_finalization)
    try:
        if recovering and plan["status"] in {"completed", "partial"}:
            # 执行器终态已可靠落盘，只补齐丢失的队列回执，不再写云盘。
            update_fs_change_plan_execution(plan_id, status=plan["status"],
                execution={**previous, "lease_generation": lease_generation},
                expected_statuses={plan["status"]}, expected_job_id=job_id,
                expected_lease_generation=int(previous["lease_generation"]))
            result["partial"] = plan["status"] == "partial"
            return result
        if (
            not client.logged_in
            or client.credential_generation != expected_generation
        ):
            raise GuangYaFSChangeStale("光鸭登录凭据已变化，请重新预览")
        if not operations:
            raise GuangYaFSChangeError("光鸭变更计划没有可执行对象")
        _validate_plan_names(operations)
        if not recovering:
            _preflight_fs_change_plan(client, operations, outcomes, cancel_check)
            execution["strm_scope"] = cloud_change_sources(client) if plan.get("trigger_strm") else None
            _append_journal(plan_id, {"action": "preflight", "status": "completed", "total": len(operations)})
        _reset_fs_checkpoint_cursor(plan_id, execution)
        update_fs_change_plan_execution(plan_id, status="running", execution=execution,
            expected_statuses={"running" if recovering else "queued"}, expected_job_id=job_id,
            expected_lease_generation=int(previous["lease_generation"]) if recovering else None)
        if recovering:
            persistence_uncertain, resumable, waiting_copies = _reconcile_fs_operations(
                client, operations, outcomes, created_targets, stats, cancel_check, stage_sources, copy_tasks)
            _reset_fs_checkpoint_cursor(plan_id, execution)
            update_fs_change_plan_execution(plan_id, status="running", execution=execution,
                expected_statuses={"running"}, expected_job_id=job_id, expected_lease_generation=lease_generation)
            if waiting_copies and not persistence_uncertain:
                raise GuangYaFSCopyPending(min(waiting_copies))
        completed_objects = {str((item.get("source") or {}).get("file_id") or item["created_path"])
            for item, outcome in zip(operations, outcomes) if outcome["status"] == "completed"}
        successful_operations = [item for item, outcome in zip(operations, outcomes) if outcome["status"] == "completed"]
        operations_by_object = {str((item.get("source") or {}).get("file_id") or item["created_path"]): (str(i), item)
                                for i, item in enumerate(operations, 1)}
        for index, item in enumerate(operations, start=1):
            if persistence_uncertain:
                break
            if outcomes[index - 1]["status"] != "not_started" and index not in resumable:
                continue
            if cancel_check is not None:
                cancel_check()
            outcome = outcomes[index - 1]
            op = str(item.get("op") or "")
            source = item.get("source") if isinstance(item.get("source"), dict) else {}
            created_id = ""
            error_type = ""
            provider_code = ""
            provider_write_started = False
            needs_checkpoint = True
            stat_key = _operation_stat_key(op)
            try:
                _preflight_operation(
                    client,
                    item,
                    created_targets=created_targets,
                    completed_objects=completed_objects,
                    source_snapshot=stage_sources.get(str(index)) if index in resumable else None,
                    cancel_check=cancel_check,
                )
                # 完成回执是历史事实，不保证子项此刻仍在原位。父目录变更前重新核对
                # 批准的前置结果；共享本次核对的目录快照，不为每个文件重复列目录。
                directory_cache: dict[str, dict[str, GuangYaFile]] = {}
                for dependency in item.get("rename_dependencies") or ():
                    if cancel_check is not None:
                        cancel_check()
                    position, predecessor = operations_by_object[dependency]
                    predecessor_id = str((created_targets.get(predecessor.get("created_path")) or {}).get("file_id") or "")
                    if predecessor["op"] == "copy":
                        valid = _copy_output(client, predecessor, created_targets, cancel_check,
                            name=str(predecessor.get("new_name") or predecessor["source"]["name"]),
                            copied_id=str((stage_sources.get(position) or {}).get("file_id") or "")) is not None
                    else:
                        valid = _verify_after(client, predecessor, predecessor_id, created_targets=created_targets,
                            cancel_check=cancel_check, stage_source=stage_sources.get(position), directory_cache=directory_cache)
                    if not valid:
                        completed_objects.discard(dependency)
                        raise GuangYaFSChangeStale("前置子项结果已变化，未执行父目录操作，请重新核对")
                provider_write_started = True
                created_id = _apply_fs_operation(client, item, outcome, created_targets, cancel_check, checkpoint, stage_sources, copy_tasks)
                needs_checkpoint = False  # 正常末阶段已持久化；异常核对/拒绝需要补记。
                stats[stat_key] += 1
                status = "completed"
            except GuangYaFSCopyPending as exc:
                outcome["reason"] = "verification_pending"
                exc.position = index
                raise
            except GuangYaFSCheckpointError:
                stats[stat_key if outcome["status"] == "completed" else "failed"] += 1
                stats["audit_failures"] += 1
                outcome["reason"] = "audit_unavailable"
                persistence_uncertain = True
                break
            except GuangYaFSChangeStale as exc:
                unmet = not set(item.get("rename_dependencies") or ()).issubset(completed_objects)
                outcome.update(status="partial" if outcome["completed_actions"] else "blocked",
                               reason="dependency_failed" if unmet else "precondition_failed")
                error_type = type(exc).__name__
                stats["precondition_failed"] += 1
                stats["failed"] += 1
                status = "failed"
            except GuangYaWriteRejected as exc:
                outcome.update(status="partial" if outcome["completed_actions"] else "failed", reason="write_rejected")
                error_type = type(exc).__name__
                provider_code = (exc.code or "")
                stats["failed"] += 1
                status = "failed"
            except Exception as exc:  # noqa: BLE001 - 单项失败需收束为可审计部分完成
                created_id = str(getattr(exc, "fs_created_id", created_id))
                if cancel_check is not None:
                    cancel_check()
                error_type = type(exc).__name__
                # provider 可能在连接中断前已经接受写入。若冻结后置条件成立，
                # 则不能再把它算成“失败后可重试”；仅审计尚未完成的 trash 保留审计缺口。
                applied = False
                if provider_write_started and (op != "copy" or copy_tasks.get(str(index))):
                    try:
                        applied = _verify_after(
                            client,
                            item,
                            created_id,
                            created_targets=created_targets,
                            copy_task_id=copy_tasks.get(str(index), "") if not stage_sources.get(str(index)) else "",
                            cancel_check=cancel_check,
                            stage_source=stage_sources.get(str(index)),
                        )
                    except GuangYaFSCopyPending as waiting:
                        outcome["reason"] = "verification_pending"
                        waiting.position = index
                        raise
                    except Exception:  # noqa: BLE001 - 后置核验失败即保持未知
                        applied = False
                if applied:
                    outcome.update(status="completed", completed_actions=list(_operation_actions(item)))
                    stats[stat_key] += 1
                    status = "completed"
                    if op == "trash" and outcome.get("reason") != "verification_pending":
                        stats["audit_failures"] += 1
                        persistence_uncertain = True
                        outcome["reason"] = "audit_unavailable"
                    else:
                        outcome.pop("reason", None)
                else:
                    if outcome.get("reason") == "verification_pending":
                        stats["verification_failed"] += 1
                    outcome.update(status="unknown" if provider_write_started else "failed")
                    outcome.setdefault("reason", "write_outcome_unknown" if provider_write_started else "execution_error")
                    stats["failed"] += 1
                    if provider_write_started:
                        # 任何写入都可能已被 Provider 接受；传输/回读失败不能
                        # 证明未执行，统一交人工核查，禁止按普通失败盲重试。
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
                outcome["reason"] = "audit_unavailable"
                stats["audit_failures"] += 1
                persistence_uncertain = True
                # 日志介质失效后停止追加写入，避免扩大无法可靠追溯的副作用面。
                break
            if status == "completed":
                completed_objects.add(str(source.get("file_id") or item["created_path"]))
                successful_operations.append(item)
            if needs_checkpoint:
                try:
                    checkpoint()
                except GuangYaFSCheckpointError:
                    stats["audit_failures"] += 1
                    persistence_uncertain = True
                    break
        return _finish_fs_change_execution(plan_id, job_id, plan, result,
            execution=execution, checkpoint=checkpoint,
            successful_operations=successful_operations, persistence_uncertain=persistence_uncertain, cancel_check=cancel_check)
    except GuangYaFSCopyPending as exc:
        if cancel_check is not None:
            try:
                cancel_check()
            except Exception as cancelled:
                setattr(cancelled, "operation_result", result)
                raise
        try:
            _write_fs_checkpoint(plan_id, job_id, execution,
                item=operations[exc.position - 1], outcome=outcomes[exc.position - 1])
        except GuangYaFSCheckpointError as failed:
            stats["audit_failures"] += 1
            result.update(partial=True, requires_manual=True)
            failed.operation_result = result
            raise
        setattr(exc, "operation_result", result)
        raise
    except Exception as exc:
        # 协作取消/写前拒绝仍沿用异常契约，但不能丢掉本次已完成与待核验事实。
        result.update(partial=any(row["status"] != "not_started" for row in outcomes),
                      requires_manual=persistence_uncertain or any(row["status"] == "unknown" for row in outcomes))
        if getattr(exc, "provider_write_not_started", False):
            for row in outcomes:
                if row["status"] in {"partial", "not_started"}:
                    row["reason"] = "cancelled"
        setattr(exc, "operation_result", result)
        raise
    finally:
        try:
            client.close()
        except Exception as exc:  # noqa: BLE001 - 关闭资源失败不能覆盖远端执行结果
            logger.warning("关闭光鸭文件变更客户端失败 type=%s", type(exc).__name__)
