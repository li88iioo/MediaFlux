"""整理删除审计：任何光鸭回收站调用都必须先持久化意图。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app import database as db
from app.logger import redact_sensitive_text


@dataclass(frozen=True)
class DeleteCandidate:
    file_id: str
    name: str
    parent_id: str
    size: int = 0
    gcid: str = ""


def _audit_fields(candidate: DeleteCandidate,
                  replacement: DeleteCandidate | None = None) -> dict:
    replacement = replacement or DeleteCandidate("", "", "", 0, "")
    return {
        "file_id": candidate.file_id,
        "file_name": candidate.name,
        "parent_id": candidate.parent_id,
        "size": candidate.size,
        "gcid": candidate.gcid,
        "replacement_file_id": replacement.file_id,
        "replacement_name": replacement.name,
        "replacement_size": replacement.size,
        "replacement_gcid": replacement.gcid,
    }


def record_blocked_delete(*, trigger: str, reason: str,
                          candidate: DeleteCandidate,
                          replacement: DeleteCandidate | None = None,
                          organize_log_id: int | None = None) -> int:
    safe_reason = redact_sensitive_text(reason or "")[:500]
    return db.add_organize_delete_audit(
        trigger=trigger, reason=safe_reason, status="blocked",
        organize_log_id=organize_log_id,
        provider_result="未调用光鸭 provider；对象保留",
        **_audit_fields(candidate, replacement),
    )


def execute_recycle_bin_delete(client, *, trigger: str, reason: str,
                               candidate: DeleteCandidate,
                               replacement: DeleteCandidate | None = None,
                               organize_log_id: int | None = None,
                               safe_failure_message: str = "",
                               delete_operation: Callable[[], object] | None = None) -> dict:
    audit_id = db.add_organize_delete_audit(
        trigger=trigger, reason=reason, status="pending",
        organize_log_id=organize_log_id,
        provider_result="等待光鸭回收站响应",
        **_audit_fields(candidate, replacement),
    )
    try:
        if delete_operation is None:
            client.delete([candidate.file_id])
        else:
            delete_operation()
    except Exception as exc:
        safe_error = (
            str(safe_failure_message or "").strip()
            or redact_sensitive_text(exc)[:500]
        )[:500]
        db.update_organize_delete_audit(
            audit_id, status="failed", error=safe_error,
            provider_result=redact_sensitive_text(
                f"光鸭回收站调用失败：{safe_error}"
            )[:500],
        )
        raise RuntimeError(safe_error) from exc
    db.update_organize_delete_audit(
        audit_id, status="success", error="",
        provider_result="光鸭 provider 已接收，文件已移入回收站",
    )
    return {"audit_id": audit_id, "status": "success"}
