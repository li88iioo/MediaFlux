"""受控补偿：复用原领域预检/执行器，不把任意工具名交给用户或模型。"""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any

from app.agent.action_history import action_history_owner_digest
from app.agent.activity_actions import _text, select_activity
from app.agent.errors import AgentToolError
from app.agent.models import ToolContext, ToolReference, ToolResult
from app.repositories import agent_compensations as receipts

# 只有提交时可提供精确写后快照的设置，才允许签发通用撤销凭证。
# 其他设置仍可通过原写工具反向修改，但不伪装成有日志保证的撤销。
_ALLOWED = frozenset({"media.set_preferences", "media.clear_preferences"})


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def compensation_candidate(
    tool_name: str, arguments: dict, preview: dict
) -> dict | None:
    """只从同一次预检的真实 before 值生成补偿参数。"""
    if tool_name not in _ALLOWED:
        return None
    data = preview.get("data", {})
    inverse = dict(arguments)
    if tool_name == "media.set_preferences" and not data.get("current", {}).get(
        "explicit", True
    ):
        return {"kind": "setting", "tool": "media.clear_preferences", "arguments": {}}
    if tool_name == "media.clear_preferences":
        # 删除无版本墓碑，不能保证清除→重建→再清除的 ABA 安全。
        return None
    current = data.get("current")
    if not isinstance(current, dict):
        return None
    for field in arguments:
        if field not in current:
            return None
        inverse[field] = current[field]
    return {"kind": "setting", "tool": tool_name, "arguments": inverse}


def _tool(name: str):
    if name not in _ALLOWED:
        raise AgentToolError("该操作不支持受控回退", code="undo_not_supported")
    from app.agent.domain_catalog import build_tool_specs

    return next(spec for spec in build_tool_specs() if spec.name == name)


def attach_undo_receipt(
    result: ToolResult, candidate: dict | None, *, key: str, context: ToolContext
) -> None:
    if not candidate or not result.ok or result.status not in {"completed", "success"}:
        return
    spec = _tool(candidate["tool"])
    arguments = spec.validator(candidate["arguments"])
    preview, expected = spec.context_confirmation_preparer(arguments, context)
    if not preview.ok or not expected:
        return
    # 必须校验原操作在提交事务中得到的写后状态，不能把晚到的新状态
    # 当成本次操作的状态冻结，否则 A→B→C 会被撤销 B 的凭证覆盖为 A。
    from app.agent.media_consumption_actions import _fingerprint
    from app.agent.media_preference_policy import PREFERENCE_FIELDS

    after = result.effect_metadata.get("compensation_after")
    if not isinstance(after, dict):
        return
    committed_expected = (
        _fingerprint(after)
        if candidate["tool"] == "media.clear_preferences"
        else _fingerprint(
            {
                "current": after,
                "proposed": {
                    field: arguments.get(field, after[field])
                    for field in PREFERENCE_FIELDS
                },
            }
        )
    )
    if not secrets.compare_digest(str(expected), committed_expected):
        return
    payload = {
        **candidate,
        "arguments": arguments,
        "expected": expected,
        "receipt_id": key,
        "preview": preview.to_dict(),
    }
    receipts.create(key, action_history_owner_digest(context.owner))
    result.references.append(ToolReference("undo_receipt", payload, ttl_seconds=86400))
    result.suggestions.append(
        "需要恢复操作前设置时，可使用本次回退凭证生成确认计划；不会撤回已经启动的任务。"
    )


def receipt_arguments(arguments: dict) -> dict:
    if not isinstance(arguments, dict) or set(arguments) != {"undo_receipt_ref"}:
        raise AgentToolError("请使用操作结果返回的回退引用")
    ref = arguments["undo_receipt_ref"]
    if (
        not isinstance(ref, str)
        or not ref.startswith("ref_")
        or not 20 <= len(ref) <= 120
    ):
        raise AgentToolError("回退引用无效")
    return dict(arguments)


def _receipt(arguments: dict) -> dict:
    payload = arguments.get("undo_receipt")
    if (
        not isinstance(payload, dict)
        or payload.get("kind") not in {"setting", "organize"}
        or not payload.get("receipt_id")
    ):
        raise AgentToolError("回退凭证无效或已失效", code="precondition_failed")
    return payload


def _organize_state(log_id: int) -> tuple[dict, str]:
    from app.modules.organize_correction import OrganizeCorrectionService

    service = OrganizeCorrectionService()
    try:
        detail = service.detail(log_id)
        if not detail["allowed_actions"]["revert"]:
            raise AgentToolError(
                "该记录没有可安全撤销的最近操作；永久删除或不完整快照不能回退",
                code="undo_not_supported",
            )
        if not service.client.logged_in:
            raise AgentToolError("光鸭尚未登录", code="not_configured")
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


def inspect_undo(arguments: dict, context: ToolContext) -> ToolResult:
    target = select_activity(arguments)
    if target["kind"] != "organize":
        return ToolResult(
            True,
            "unsupported",
            "该活动没有可安全回退的文件操作快照",
            data={"reversible": False},
            suggestions=[
                "下载请求不能通过撤销收回；本地文件没有完整反向快照时请人工检查。"
            ],
        )
    public, expected = _organize_state(target["id"])
    receipt_id = _hash(
        {
            "owner": context.owner,
            "session": context.session_id,
            "target": target,
            "expected": expected,
        }
    )
    receipts.create(receipt_id, action_history_owner_digest(context.owner))
    return ToolResult(
        True,
        "success",
        "找到可回退的最近文件移动/改名快照，尚未执行",
        data={"reversible": True, **public},
        references=[
            ToolReference(
                "undo_receipt",
                {
                    "kind": "organize",
                    "receipt_id": receipt_id,
                    "target": target,
                    "expected": expected,
                    "version": public["version"],
                },
                ttl_seconds=86400,
            )
        ],
    )


def prepare_undo(arguments: dict, context: ToolContext) -> tuple[ToolResult, str]:
    payload = _receipt(arguments)
    if (
        receipts.state(
            payload["receipt_id"], action_history_owner_digest(context.owner)
        )
        != "available"
    ):
        raise AgentToolError(
            "回退凭证已执行、结果待核对或已失效", code="confirmation_stale"
        )
    if payload["kind"] == "setting":
        spec = _tool(payload["tool"])
        normalized = spec.validator(payload["arguments"])
        preview, current = spec.context_confirmation_preparer(normalized, context)
        if not preview.ok:
            raise AgentToolError(
                "原对象已变化，不能直接回退", code="confirmation_stale"
            )
        public = preview.data
    else:
        public, current = _organize_state(payload["target"]["id"])
    if not secrets.compare_digest(str(current), str(payload["expected"])):
        raise AgentToolError(
            "操作后对象已发生变化，请重新核对；不会覆盖后续修改",
            code="confirmation_stale",
        )
    return ToolResult(
        True,
        "confirmation_required",
        "确认后回退这一项可逆操作",
        data={
            "kind": payload["kind"],
            "changes": public,
            "effects": [
                "只恢复冻结快照中的设置或文件位置",
                "已触发的下载/通知不会被撤回",
                "执行前再次校验对象，凭证只可消费一次",
            ],
        },
    ), _hash(payload)


def execute_undo(
    arguments: dict, expected_context: str, context: ToolContext
) -> ToolResult:
    payload = _receipt(arguments)
    _, fingerprint = prepare_undo(arguments, context)
    if not secrets.compare_digest(fingerprint, expected_context):
        raise AgentToolError("回退计划已变化", code="confirmation_stale")
    digest = action_history_owner_digest(context.owner)
    service = None
    # 先占用凭证；此处尚未创建外部 client，数据库失败不会泄漏服务。
    if not receipts.claim(payload["receipt_id"], digest):
        raise AgentToolError("回退已经执行或正在执行", code="confirmation_stale")
    try:
        if payload["kind"] == "setting":
            spec = _tool(payload["tool"])
            normalized = spec.validator(payload["arguments"])
            result = spec.context_confirmed_handler(
                normalized, payload["expected"], context
            )
            if spec.post_write_verifier and result.ok:
                result = spec.post_write_verifier(normalized, result)
            receipts.finish(payload["receipt_id"], digest, completed=result.ok)
            return ToolResult(
                result.ok,
                result.status,
                "原设置已恢复" if result.ok else "回退未能完成，请核对实际状态",
                data={"result": result.to_dict(), "restored": result.ok},
                error=result.error,
            )
        from app.modules.organize_tasks import get_organize_manager

        public, current_fingerprint = _organize_state(payload["target"]["id"])
        if not secrets.compare_digest(current_fingerprint, payload["expected"]):
            raise AgentToolError(
                "整理操作在确认后发生变化，请重新预检", code="confirmation_stale"
            )
        frozen_version = payload["version"]
        if public["version"] != frozen_version:
            raise AgentToolError(
                "整理版本已变化，请重新预检", code="confirmation_stale"
            )
        from app.modules.organize_correction import OrganizeCorrectionService

        service = OrganizeCorrectionService()
        operation_token = "undo-" + payload["receipt_id"][:48]

        def operation():
            try:
                value = service.revert_latest(
                    payload["target"]["id"], operation_token, frozen_version
                )
                receipts.finish(
                    payload["receipt_id"], digest, completed=bool(value.get("success"))
                )
                return value
            except BaseException:
                receipts.finish(payload["receipt_id"], digest, completed=False)
                raise
            finally:
                service.close()

        accepted = get_organize_manager().start_operation(
            "回退最近操作", public["title"] or "Agent 回退", operation
        )
        if not accepted.get("ok"):
            service.close()
            receipts.finish(payload["receipt_id"], digest, completed=False)
            return ToolResult(
                False,
                "busy",
                "整理队列未接收回退任务，请核对后重试",
                error="整理任务正在运行",
            )
        return ToolResult(
            True,
            "accepted",
            "回退任务已交给整理执行器，尚未完成",
            data={"accepted": True, "affected": public["affected"]},
            references=[
                ToolReference(
                    "activity_selection",
                    {"items": [payload["target"]]},
                    ttl_seconds=86400,
                )
            ],
        )
    except BaseException:
        if service:
            service.close()
        receipts.finish(payload["receipt_id"], digest, completed=False)
        raise
