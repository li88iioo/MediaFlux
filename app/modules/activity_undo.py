"""媒体偏好与整理快照的条件补偿；凭证由宿主签发并绑定身份。"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any

from app.database import media_owner_digest
from app.modules.media_preferences import (
    apply_media_preferences_change, media_preferences_preview, prepare_media_preferences_change,
    public_media_preferences, snapshot_media_preferences_change, validate_preference_updates,
)
from app.repositories import agent_compensations as receipts
from app.repositories.activity import public_text as _text


class UndoError(ValueError):
    def __init__(self, message: str, *, code: str = "precondition_failed"):
        super().__init__(message)
        self.code = code


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def _owner_digest(owner: str) -> str:
    if not isinstance(owner, str) or not owner.strip():
        raise UndoError("无法确认当前身份", code="identity_required")
    return media_owner_digest(owner)


def preference_compensation(tool_name: str, arguments: dict, preview: dict) -> dict | None:
    """只从同次预检生成反向变更；清除没有版本墓碑，不能签发 ABA 不安全的回退。"""
    if tool_name != "preferences.update":
        return None
    current = preview.get("data", {}).get("current")
    if not isinstance(current, dict):
        return None
    if not current.get("explicit", True):
        return {"kind": "media_preferences", "updates": None}
    if any(field not in current for field in arguments):
        return None
    return {"kind": "media_preferences", "updates": {field: current[field] for field in arguments}}


def _preference_updates(payload: dict) -> dict | None:
    # 只读取历史载荷，不解析或调用历史 action。
    if payload["kind"] == "media_preferences":
        updates = payload["updates"]
        return None if updates is None else validate_preference_updates(updates)
    name, arguments = payload["tool"], payload["arguments"]
    if name in {"preferences.clear", "media.clear_preferences"}:
        if arguments:
            raise UndoError("清除偏好回退凭证包含无效参数")
        return None
    if name not in {"preferences.update", "media.set_preferences"}:
        raise UndoError("该操作不支持受控回退", code="undo_not_supported")
    return validate_preference_updates(arguments)


def _receipt(receipt_id: str, owner: str) -> dict:
    digest = _owner_digest(owner)
    if not isinstance(receipt_id, str) or not receipt_id.strip() or len(receipt_id) > 200:
        raise UndoError("请使用操作返回的真实 receipt_id")
    stored = receipts.get(receipt_id, digest)
    if stored is not None and stored["payload"] is None:
        raise UndoError("历史回执未保存私有快照，已失效；整理请重新 inspect，媒体偏好请核对当前设置", code="confirmation_stale")
    if (stored is None or stored["state"] != "available" or stored["expires_at"] <= time.time()):
        raise UndoError("回退凭证已执行、结果待核对或已失效，请重新核对", code="confirmation_stale")
    payload = stored["payload"]
    if not isinstance(payload, dict) or payload.get("kind") not in {"setting", "media_preferences", "organize"}:
        raise UndoError("回退凭证没有有效私有快照", code="confirmation_stale")
    return {**payload, "receipt_id": receipt_id}


def preference_undo_receipt(candidate: dict | None, after: dict | None, *, key: str, owner: str) -> str | None:
    """写后快照必须来自原事务；晚到的新版本不能冒充本次写入。"""
    if candidate is None or not isinstance(after, dict):
        return None
    updates = _preference_updates(candidate)
    prepared = prepare_media_preferences_change(_owner_digest(owner), updates)
    if updates is None and not prepared["current"]["explicit"]:
        return None
    expected = snapshot_media_preferences_change(after, updates)["fingerprint"]
    if not secrets.compare_digest(prepared["fingerprint"], expected):
        return None
    return receipts.create(key, _owner_digest(owner), payload={**candidate, "expected": expected})


def _organize_state(log_id: int) -> tuple[dict, str]:
    from app.modules.organize_correction import OrganizeCorrectionService

    service = OrganizeCorrectionService()
    try:
        detail = service.detail(log_id)
        if not detail["allowed_actions"]["revert"]:
            raise UndoError(
                "该记录没有可安全撤销的最近操作；永久删除或不完整快照不能回退",
                code="undo_not_supported",
            )
        if not service.client.logged_in:
            raise UndoError("光鸭尚未登录", code="not_configured")
        try:
            detail = service.preflight_revert(log_id)["detail"]
        except (ValueError, RuntimeError) as exc:
            raise UndoError(str(exc)) from exc
        fingerprint = _hash(
            {
                "id": log_id,
                "version": detail["version"],
                "status": detail["status"],
                "items": detail["items"],
                "operations": detail["operations"],
            }
        )
        successful = [
            step
            for step in detail["operations"]
            if step.get("action") == "move_rename" and step.get("status") == "success"
        ]
        previous = successful[0]["operation_token"]
        samples = [
            {"from": _text(step.get("to_name")), "to": _text(step.get("from_name"))}
            for step in successful
            if step["operation_token"] == previous
        ]
        return {
            "title": _text(detail.get("title"), 120),
            "version": detail["version"],
            "changes": samples[:20],
            "affected": len(samples),
        }, fingerprint
    finally:
        service.close()


def inspect_undo(log_id: int, *, owner: str) -> dict:
    digest = _owner_digest(owner)
    if type(log_id) is not int or log_id < 1:
        raise UndoError("log_id 必须是正整数整理日志编号")
    public, expected = _organize_state(log_id)
    receipt_id = _hash({"owner": digest, "log_id": log_id, "expected": expected})
    receipt_id = receipts.create(receipt_id, digest, payload={"kind": "organize", "log_id": log_id,
                                 "expected": expected, "version": public["version"]})
    _receipt(receipt_id, owner)
    return {"ok": True, "status": "success", "summary": "已核对可撤销的文件移动/改名；以下为撤销预览（当前名称 → 撤回后的名称），尚未执行",
            "data": {"reversible": True, "log_id": log_id, **public, "receipt_id": receipt_id}}


def prepare_undo(receipt_id: str, *, owner: str) -> dict:
    payload = _receipt(receipt_id, owner)
    if payload["kind"] in {"setting", "media_preferences"}:
        updates = _preference_updates(payload)
        prepared = prepare_media_preferences_change(_owner_digest(owner), updates)
        if updates is None and not prepared["current"]["explicit"]:
            raise UndoError("原对象已变化，不能直接回退", code="confirmation_stale")
        public, current = media_preferences_preview(prepared), prepared["fingerprint"]
    else:
        public, current = _organize_state(payload["log_id"])
    if not secrets.compare_digest(current, str(payload["expected"])):
        raise UndoError("操作后对象已发生变化，请重新核对；不会覆盖后续修改", code="confirmation_stale")
    return {"preview": {"ok": True, "status": "confirmation_required", "summary": "确认后回退这一项可逆操作",
                        "data": {"kind": payload["kind"], "changes": public,
                                 "effects": ["只恢复冻结快照中的设置或文件位置", "已触发的下载/通知不会被撤回",
                                             "执行前再次校验对象，凭证只可消费一次"]}},
            "fingerprint": _hash(payload)}


def execute_undo(receipt_id: str, *, expected_fingerprint: str, owner: str) -> dict:
    prepared = prepare_undo(receipt_id, owner=owner)
    if not secrets.compare_digest(prepared["fingerprint"], expected_fingerprint):
        raise UndoError("回退计划已变化", code="confirmation_stale")
    payload, digest = _receipt(receipt_id, owner), _owner_digest(owner)
    # 先占用凭证，数据库失败不会创建外部 client。
    if not receipts.claim(payload["receipt_id"], digest):
        raise UndoError("回退已经执行或正在执行", code="confirmation_stale")
    service = None
    try:
        if payload["kind"] in {"setting", "media_preferences"}:
            updates = _preference_updates(payload)
            updated = apply_media_preferences_change(digest, updates, expected_fingerprint=payload["expected"])
            if updated is None:
                raise UndoError("媒体偏好已变化，请重新预检", code="confirmation_stale")
            receipts.finish(payload["receipt_id"], digest, completed=True)
            return {"ok": True, "status": "completed", "summary": "原设置已恢复",
                    "data": {"restored": True, "result": {"ok": True, "status": "completed", "summary": "显式媒体偏好已恢复",
                             "data": {"operation": "set_preferences" if updates is not None else "clear_preferences", "affected": 1,
                                      **(public_media_preferences(updated) if updates is not None else {})}}}}
        from app.modules.organize_correction import OrganizeCorrectionService
        from app.modules.organize_tasks import get_organize_manager

        log_id = payload["log_id"]
        public, current = _organize_state(log_id)
        if not secrets.compare_digest(current, payload["expected"]) or public["version"] != payload["version"]:
            raise UndoError("整理操作在确认后发生变化，请重新预检", code="confirmation_stale")
        service = OrganizeCorrectionService()

        def operation():
            try:
                value = service.revert_latest(log_id, "undo-" + payload["receipt_id"][:48], payload["version"])
                receipts.finish(payload["receipt_id"], digest, completed=bool(value.get("success")))
                return value
            except BaseException:
                receipts.finish(payload["receipt_id"], digest, completed=False)
                raise
            finally:
                service.close()

        accepted = get_organize_manager().start_operation("回退最近操作", public["title"] or "回退", operation)
        if not accepted.get("ok"):
            receipts.release_unaccepted(payload["receipt_id"], digest)
            service.close()
            return {"ok": False, "status": "busy", "summary": "整理队列未接收回退任务，请核对后重试", "error": "整理任务正在运行"}
        return {"ok": True, "status": "accepted", "summary": "回退任务已交给整理执行器，尚未完成",
                "data": {"accepted": True, "affected": public["affected"], "log_id": log_id,
                         "task_id": str(accepted.get("task_id") or "").strip()}}
    except BaseException:
        if service:
            service.close()
        receipts.finish(payload["receipt_id"], digest, completed=False)
        raise
