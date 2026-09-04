"""Agent 光鸭通用文件变更：冻结预览、人工确认与持久执行。"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.agent.confirmation import confirmation_context_fingerprint
from app.agent.errors import AgentToolError
from app.agent.guangya_workspace_actions import latest_guangya_observation_ref
from app.agent.models import Evidence, ToolContext, ToolResult
from app.agent.session_context import (
    AgentContextWriteGuard,
    AgentSessionContextRepository,
)
from app.clients.guangya import GuangYaClient
from app.modules.guangya_fs_change import (
    GuangYaFSChangeError,
    GuangYaFSChangeStale,
    build_fs_change_plan,
    confirm_fs_change_plan,
    discard_fs_change_plan,
    execute_fs_change_plan,
    load_fs_change_plan,
)
from app.modules.guangya_workspace import (
    GuangYaWorkspaceError,
    GuangYaWorkspaceStale,
    load_directory_observation_set,
    valid_object_handle,
    valid_observation_ref,
)
from app.repositories.organize_operation_jobs import (
    get_organize_operation_job,
    organize_operation_owner_digest,
    organize_operation_public_ref,
    organize_operation_queue_position,
)

logger = logging.getLogger(__name__)
_CONTEXT_TYPE = "guangya_fs_change"
_TTL_SECONDS = 10 * 60.0
_PLAN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass
class _Flow:
    owner: str
    plan_id: str
    fingerprint: str
    preview_safe: dict[str, Any]
    generation: int = 0
    revision: int = 0
    updated_at: float = 0.0


_lock = threading.RLock()
_flows: dict[str, _Flow] = {}
_repository: AgentSessionContextRepository | None = None


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def configure_guangya_fs_change_context(
    repository: AgentSessionContextRepository | None,
) -> None:
    global _repository
    _repository = repository


def reset_guangya_fs_change_context_for_tests() -> None:
    global _repository
    with _lock:
        _flows.clear()
    _repository = None


def clear_guangya_fs_change_context(*, owner: str) -> None:
    """清除 owner 的内存缓存；SQLite epoch 是唯一有效性权威。"""
    owner_key = str(owner or "").strip()
    if not owner_key:
        return
    with _lock:
        _flows.pop(owner_key, None)


def _safe_preview(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    try:
        counts = {
            key: max(0, int(value.get(key) or 0))
            for key in (
                "total",
                "rename_count",
                "move_count",
                "relocate_count",
                "trash_count",
                "create_directory_count",
            )
        }
    except (TypeError, ValueError, OverflowError):
        return None
    samples: list[str] = []
    raw_samples = value.get("sample_changes")
    if not isinstance(raw_samples, list):
        return None
    for raw in raw_samples[:6]:
        sample = str(raw or "").strip()
        if sample:
            samples.append(sample[:520])
    return {
        **counts,
        "sample_changes": samples,
        "trigger_strm": bool(value.get("trigger_strm")),
        "cloud_write": False,
    }


def _flow_payload(flow: _Flow) -> dict[str, Any]:
    return {
        "plan_id": flow.plan_id,
        "fingerprint": flow.fingerprint,
        "preview_safe": dict(flow.preview_safe),
    }


def _flow_from_payload(
    owner: str,
    payload: dict[str, Any],
    *,
    generation: int = 0,
    revision: int = 0,
) -> _Flow | None:
    plan_id = str(payload.get("plan_id") or "").strip()
    fingerprint = str(payload.get("fingerprint") or "").strip()
    preview_safe = _safe_preview(payload.get("preview_safe"))
    if (
        not _PLAN_ID_RE.fullmatch(plan_id)
        or not _FINGERPRINT_RE.fullmatch(fingerprint)
        or preview_safe is None
    ):
        return None
    return _Flow(
        owner=str(owner),
        plan_id=plan_id,
        fingerprint=fingerprint,
        preview_safe=preview_safe,
        generation=max(0, int(generation or 0)),
        revision=max(0, int(revision or 0)),
        updated_at=time.monotonic(),
    )


def _begin_update(owner: str) -> tuple[_Flow | None, AgentContextWriteGuard]:
    if _repository is not None:
        begin_update = getattr(_repository, "begin_context_update", None)
        if callable(begin_update):
            persisted, guard = begin_update(owner=owner, context_type=_CONTEXT_TYPE)
            previous = None
            if persisted is not None:
                previous = _flow_from_payload(
                    owner,
                    persisted.payload,
                    generation=persisted.generation,
                    revision=persisted.revision,
                )
                if previous is not None:
                    with _lock:
                        _flows[owner] = previous
            return previous, guard
        begin = getattr(_repository, "begin_context", None)
        if callable(begin):
            return _flow(owner), begin(owner=owner, context_type=_CONTEXT_TYPE)
    return _flow(owner), AgentContextWriteGuard(0, 0)


def _save(flow: _Flow) -> bool:
    flow.updated_at = time.monotonic()
    if _repository is not None:
        try:
            guarded = getattr(_repository, "replace_latest_guarded", None)
            if callable(guarded):
                if flow.generation <= 0:
                    return False
                persisted = guarded(
                    owner=flow.owner,
                    context_type=_CONTEXT_TYPE,
                    payload=_flow_payload(flow),
                    expires_at=time.time() + _TTL_SECONDS,
                    guard=AgentContextWriteGuard(flow.generation, flow.revision),
                )
                if persisted is None:
                    return False
                flow.generation = persisted.generation
                flow.revision = persisted.revision
            else:
                _repository.replace_latest(
                    owner=flow.owner,
                    context_type=_CONTEXT_TYPE,
                    payload=_flow_payload(flow),
                    expires_at=time.time() + _TTL_SECONDS,
                )
        except Exception as exc:  # noqa: BLE001 - 上下文存储失败必须安全降级
            logger.warning("Agent 光鸭变更上下文保存失败 type=%s", type(exc).__name__)
            return False
    with _lock:
        _flows[flow.owner] = flow
    return True


def _flow(owner: str) -> _Flow | None:
    owner_key = str(owner or "").strip()
    if not owner_key:
        return None
    if _repository is not None:
        try:
            persisted = _repository.get_latest(
                owner=owner_key, context_type=_CONTEXT_TYPE, now=time.time()
            )
            if persisted is not None:
                flow = _flow_from_payload(
                    owner_key,
                    persisted.payload,
                    generation=persisted.generation,
                    revision=persisted.revision,
                )
                if flow is not None:
                    with _lock:
                        _flows[owner_key] = flow
                    return flow
        except Exception as exc:  # noqa: BLE001 - 上下文读取失败按无预览处理
            logger.warning("Agent 光鸭变更上下文读取失败 type=%s", type(exc).__name__)
        with _lock:
            _flows.pop(owner_key, None)
        return None
    with _lock:
        current = _flows.get(owner_key)
        if current is None or time.monotonic() - current.updated_at > _TTL_SECONDS:
            _flows.pop(owner_key, None)
            return None
        return current


def _consume(flow: _Flow) -> bool:
    if _repository is not None:
        try:
            consume = getattr(_repository, "consume_latest_guarded", None)
            if callable(consume) and flow.generation > 0 and flow.revision > 0:
                consumed = bool(
                    consume(
                        owner=flow.owner,
                        context_type=_CONTEXT_TYPE,
                        guard=AgentContextWriteGuard(flow.generation, flow.revision),
                    )
                )
            elif not callable(consume):
                consumed = bool(
                    _repository.delete_latest(
                        owner=flow.owner, context_type=_CONTEXT_TYPE
                    )
                )
            else:
                consumed = False
        except Exception as exc:  # noqa: BLE001 - 消费失败需保留可见警告
            logger.warning("Agent 光鸭变更上下文消费失败 type=%s", type(exc).__name__)
            return False
        if not consumed:
            return False
    with _lock:
        _flows.pop(flow.owner, None)
    return True


def _normalize_path(value: object, *, field: str) -> str:
    path = str(value or "").strip().replace("\\", "/")
    if not path:
        raise AgentToolError(f"{field} 不能为空")
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 2048 or any(part in {".", ".."} for part in path.split("/") if part):
        raise AgentToolError(f"{field} 必须是精确光鸭绝对路径")
    parts = [part for part in path.split("/") if part]
    return "/" + "/".join(parts) if parts else "/"


def guangya_fs_change_preview_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("光鸭变更预览参数必须是对象")
    if set(arguments) - {"observation_ref", "operations", "trigger_strm"}:
        raise AgentToolError("光鸭变更预览包含不支持的参数")
    ref = str(arguments.get("observation_ref") or "").strip().upper()
    if ref and not valid_observation_ref(ref):
        raise AgentToolError("observation_ref 格式无效")
    raw_operations = arguments.get("operations")
    if not isinstance(raw_operations, list) or not 1 <= len(raw_operations) <= 200:
        raise AgentToolError("operations 必须包含 1 到 200 项操作")
    operations: list[dict[str, Any]] = []
    effective_operation_count = 0
    for raw in raw_operations:
        if not isinstance(raw, dict):
            raise AgentToolError("光鸭变更操作必须是对象")
        op = str(raw.get("op") or "").strip().casefold()
        expected = {
            "rename": {"op", "object_ref", "new_name"},
            "move": {"op", "object_ref", "target_path"},
            "copy": {"op", "object_ref", "target_path"},
            "relocate": {"op", "object_ref", "target_path", "new_name"},
            "trash": {"op", "object_ref"},
            "create_directory": {"op", "parent_path", "name"},
        }.get(op)
        if op == "batch_relocate":
            allowed = {
                "op",
                "items",
                "target_path",
                "title",
                "naming",
                "season",
                "episode_padding",
            }
            if set(raw) - allowed or not {
                "op",
                "items",
                "target_path",
            }.issubset(raw):
                raise AgentToolError("光鸭批量规整操作字段无效")
            raw_items = raw.get("items")
            if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= 200:
                raise AgentToolError("batch_relocate.items 必须包含 1 到 200 项")
            items: list[dict[str, Any]] = []
            seen_refs: set[str] = set()
            for item in raw_items:
                if not isinstance(item, dict) or set(item) != {
                    "object_ref",
                    "episode",
                }:
                    raise AgentToolError("批量规整条目字段无效")
                object_ref = str(item.get("object_ref") or "").strip().upper()
                if not valid_object_handle(object_ref) or object_ref in seen_refs:
                    raise AgentToolError("批量规整 object_ref 无效或重复")
                episode = item.get("episode")
                if type(episode) is not int or not 1 <= episode <= 9999:
                    raise AgentToolError("批量规整 episode 必须在 1 到 9999 之间")
                seen_refs.add(object_ref)
                items.append({"object_ref": object_ref, "episode": episode})
            target_path = _normalize_path(raw.get("target_path"), field="target_path")
            title = raw.get("title")
            if title in {None, ""}:
                title = target_path.rsplit("/", 1)[-1]
            if not isinstance(title, str) or not 1 <= len(title.strip()) <= 180:
                raise AgentToolError("批量规整 title 长度必须在 1 到 180 之间")
            naming = str(raw.get("naming") or "absolute").strip().casefold()
            if naming not in {"season_episode", "absolute"}:
                raise AgentToolError("batch_relocate.naming 无效")
            season = raw.get("season", 1)
            if type(season) is not int or not 0 <= season <= 999:
                raise AgentToolError("batch_relocate.season 必须在 0 到 999 之间")
            episode_padding = raw.get("episode_padding", 2)
            if type(episode_padding) is not int or not 2 <= episode_padding <= 4:
                raise AgentToolError("episode_padding 必须在 2 到 4 之间")
            normalized = {
                "op": op,
                "items": items,
                "target_path": target_path,
                "title": title.strip(),
                "naming": naming,
                "season": season,
                "episode_padding": episode_padding,
            }
            operations.append(normalized)
            effective_operation_count += len(items)
            continue
        if op == "create_directory" and set(raw) == {"op", "path"}:
            path = _normalize_path(raw.get("path"), field="path")
            if path == "/":
                raise AgentToolError("不能把根目录作为新建目录")
            parent_path, _, name = path.rpartition("/")
            raw = {
                "op": op,
                "parent_path": parent_path or "/",
                "name": name,
            }
            expected = {"op", "parent_path", "name"}
        if expected is None or set(raw) != expected:
            raise AgentToolError("光鸭变更操作字段与 op 不匹配")
        normalized: dict[str, Any] = {"op": op}
        if op != "create_directory":
            object_ref = str(raw.get("object_ref") or "").strip().upper()
            if not valid_object_handle(object_ref):
                raise AgentToolError("object_ref 格式无效")
            normalized["object_ref"] = object_ref
        if op in {"rename", "relocate"}:
            name = raw.get("new_name")
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 255:
                raise AgentToolError("new_name 长度必须在 1 到 255 之间")
            normalized["new_name"] = name.strip()
        if op in {"move", "relocate", "copy"}:
            normalized["target_path"] = _normalize_path(
                raw.get("target_path"), field="target_path"
            )
        elif op == "create_directory":
            normalized["parent_path"] = _normalize_path(
                raw.get("parent_path"), field="parent_path"
            )
            name = raw.get("name")
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 255:
                raise AgentToolError("name 长度必须在 1 到 255 之间")
            normalized["name"] = name.strip()
        operations.append(normalized)
        effective_operation_count += 1
    if effective_operation_count > 200:
        raise AgentToolError("展开后的光鸭变更操作不能超过 200 项")
    trigger_strm = arguments.get("trigger_strm", True)
    if type(trigger_strm) is not bool:
        raise AgentToolError("trigger_strm 必须是布尔值")
    return {
        "observation_ref": ref,
        "operations": operations,
        "trigger_strm": trigger_strm,
    }


def guangya_fs_change_execute_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or arguments:
        raise AgentToolError("执行光鸭变更不接受新参数")
    return {}


def _public_error(exc: Exception) -> AgentToolError:
    if isinstance(exc, (GuangYaFSChangeStale, GuangYaWorkspaceStale)):
        return AgentToolError(str(exc), code="confirmation_stale")
    if isinstance(exc, (GuangYaFSChangeError, GuangYaWorkspaceError)):
        return AgentToolError(str(exc), code="precondition_failed")
    logger.warning("Agent 光鸭通用变更失败 type=%s", type(exc).__name__)
    return AgentToolError("光鸭变更计划当前不可用", code="unavailable")


def _operation_object_refs(operations: list[dict[str, Any]]) -> tuple[str, ...]:
    refs: list[str] = []
    for operation in operations:
        if str(operation.get("op") or "").strip().casefold() == "batch_relocate":
            refs.extend(
                str(item.get("object_ref") or "").strip().upper()
                for item in operation.get("items") or ()
                if isinstance(item, dict)
            )
            continue
        object_ref = str(operation.get("object_ref") or "").strip().upper()
        if object_ref:
            refs.append(object_ref)
    return tuple(dict.fromkeys(refs))


def preview_guangya_fs_change(
    arguments: dict[str, Any], context: ToolContext
) -> ToolResult:
    if not context.owner:
        raise AgentToolError("光鸭变更预览需要已登录会话", code="precondition_failed")
    ref = str(arguments.get("observation_ref") or "").strip().upper()
    if not ref:
        ref = latest_guangya_observation_ref(context.owner)
    if not ref:
        raise AgentToolError("请先读取要操作的光鸭目录", code="precondition_failed")
    previous, guard = _begin_update(context.owner)
    client: GuangYaClient | None = None
    plan: dict[str, Any] | None = None
    try:
        observation = load_directory_observation_set(
            ref,
            owner=context.owner,
            object_refs=_operation_object_refs(list(arguments["operations"])),
        )
        client = GuangYaClient()
        if not client.logged_in:
            raise AgentToolError("光鸭账号尚未连接", code="precondition_failed")
        plan = build_fs_change_plan(
            client,
            owner=context.owner,
            observation=observation,
            operations=list(arguments["operations"]),
            trigger_strm=bool(arguments["trigger_strm"]),
        )
    except AgentToolError:
        raise
    except Exception as exc:
        raise _public_error(exc) from exc
    finally:
        if client is not None:
            client.close()
    if plan is None:
        raise AgentToolError("光鸭变更计划当前不可用", code="unavailable")
    stats = plan.get("stats") if isinstance(plan.get("stats"), dict) else {}
    preview_safe = {
        "total": max(0, int(stats.get("total") or 0)),
        "rename_count": max(0, int(stats.get("rename") or 0)),
        "move_count": max(0, int(stats.get("move") or 0)),
        "relocate_count": max(0, int(stats.get("relocate") or 0)),
        "copy_count": max(0, int(stats.get("copy") or 0)),
        "trash_count": max(0, int(stats.get("trash") or 0)),
        "create_directory_count": max(0, int(stats.get("create_directory") or 0)),
        "sample_changes": [
            str(item)[:520] for item in list(plan.get("samples") or [])[:6]
        ],
        "trigger_strm": bool(plan.get("trigger_strm")),
        "cloud_write": False,
    }
    flow = _Flow(
        owner=context.owner,
        plan_id=str(plan["plan_id"]),
        fingerprint=str(plan["fingerprint"]),
        preview_safe=preview_safe,
        generation=guard.generation,
        revision=guard.revision,
    )
    if not _save(flow):
        discard_fs_change_plan(flow.plan_id, preview_only=True)
        raise AgentToolError(
            "光鸭变更预览已被更新请求取代，请重新生成",
            code="precondition_failed",
        )
    if previous is not None and previous.plan_id != flow.plan_id:
        # 旧 flow 可能已被另一请求确认并成功入队；只能清理仍处于 previewed
        # 的孤立预览，绝不能删除 queued/running 的冻结执行凭据。
        discard_fs_change_plan(previous.plan_id, preview_only=True)
    return ToolResult(
        True,
        "ready",
        f"已冻结 {preview_safe['total']} 项光鸭文件变更，尚未写入云盘",
        data=preview_safe,
        evidence=[
            Evidence(
                "guangya_fs_change_plan",
                "计划已绑定当前会话、登录凭据世代和短时观察快照；公开结果不含对象 ID 或完整云端路径。",
                _now(),
            )
        ],
        suggestions=["确认前请核对变更类型和名称示例；执行工具不能追加新操作。"],
    )


def _confirmation_fingerprint(flow: _Flow, plan: dict[str, Any]) -> str:
    return confirmation_context_fingerprint(
        {
            "owner": flow.owner,
            "plan_id": flow.plan_id,
            "plan_fingerprint": flow.fingerprint,
            "credential_generation": int(plan.get("credential_generation") or 0),
            **flow.preview_safe,
        },
        domain="guangya-fs-change-confirmation",
    )


def prepare_guangya_fs_change_confirmation(
    _arguments: dict[str, Any], context: ToolContext
) -> tuple[ToolResult, str]:
    flow = _flow(context.owner)
    if flow is None:
        raise AgentToolError(
            "最近光鸭变更预览不存在或已过期", code="precondition_failed"
        )
    client: GuangYaClient | None = None
    try:
        plan = load_fs_change_plan(
            flow.plan_id,
            owner=context.owner,
            expected_fingerprint=flow.fingerprint,
        )
        client = GuangYaClient()
        raw_generation = plan.get("credential_generation")
        expected_generation = int(raw_generation) if raw_generation is not None else -1
        if not client.logged_in or int(client.credential_generation) != int(
            expected_generation
        ):
            raise GuangYaFSChangeStale("光鸭登录凭据已变化，请重新预览")
    except Exception as exc:
        raise _public_error(exc) from exc
    finally:
        if client is not None:
            client.close()
    return ToolResult(
        True,
        "confirmation_required",
        f"确认后将执行 {flow.preview_safe['total']} 项光鸭文件变更",
        data={
            **flow.preview_safe,
            "effects": [
                "只执行刚才冻结的对象、名称和目标目录，不会扩大范围。",
                "移动、复制和改名会在写前重新检查快照与同名冲突。",
                "trash 只调用 Provider 回收站语义，不提供永久删除。",
                "每项写入后都会重新读取目录或对象状态验证真实结果。",
                *(
                    ["至少一项成功后会触发 STRM 全量核对。"]
                    if flow.preview_safe["trigger_strm"]
                    else []
                ),
            ],
        },
        evidence=[
            Evidence(
                "guangya_fs_change_plan",
                "已重新核对当前会话的冻结计划、计划签名和登录凭据世代。",
                _now(),
            )
        ],
    ), _confirmation_fingerprint(flow, plan)


def execute_guangya_fs_change(_arguments: dict[str, Any]) -> ToolResult:
    raise AgentToolError("光鸭文件变更必须先预览并确认", code="confirmation_required")


def execute_guangya_fs_change_confirmed(
    _arguments: dict[str, Any], expected_context: str, context: ToolContext
) -> ToolResult:
    flow = _flow(context.owner)
    if flow is None:
        raise AgentToolError(
            "光鸭变更预览已过期，请重新生成", code="confirmation_stale"
        )
    try:
        plan = load_fs_change_plan(
            flow.plan_id,
            owner=context.owner,
            expected_fingerprint=flow.fingerprint,
        )
        if _confirmation_fingerprint(flow, plan) != str(expected_context or ""):
            raise AgentToolError(
                "光鸭变更预览已变化，请重新预检", code="confirmation_stale"
            )
        confirmed = confirm_fs_change_plan(
            flow.plan_id,
            owner=context.owner,
            expected_fingerprint=flow.fingerprint,
        )
        from app.modules.organize_tasks import get_organize_manager

        task = get_organize_manager().start_durable_operation(
            "光鸭文件变更",
            "已确认通用文件变更冻结计划",
            job_kind="agent_guangya_fs_change",
            owner=context.owner,
            payload={
                "version": 1,
                "plan_id": flow.plan_id,
                "plan_fingerprint": flow.fingerprint,
                "owner_digest": organize_operation_owner_digest(context.owner),
                "credential_generation": int(confirmed["credential_generation"]),
            },
            dedupe_key=f"agent-guangya-fs-change:{flow.plan_id}:{flow.fingerprint}",
        )
    except AgentToolError:
        raise
    except Exception as exc:
        raise _public_error(exc) from exc
    if not task.get("ok"):
        # enqueue 已在数据库事务提交前把 job_id 写入计划。即使当前进程在
        # 随后的即时 claim/dispatcher 启动阶段报错，另一个进程仍可能已领取
        # 同一持久任务；此时不能误导用户重提一份重复远端写入。
        try:
            persisted = load_fs_change_plan(
                flow.plan_id,
                owner=context.owner,
                expected_fingerprint=flow.fingerprint,
            )
            persisted_job_id = str(persisted.get("job_id") or "")
            row = (
                get_organize_operation_job(persisted_job_id)
                if _PLAN_ID_RE.fullmatch(persisted_job_id)
                else None
            )
        except Exception as exc:
            raise AgentToolError(
                "光鸭操作队列状态当前不可用，请稍后查询任务状态",
                code="unavailable",
            ) from exc
        persisted_status = str(row["status"] or "") if row is not None else ""
        if persisted_status in {"pending", "running"}:
            task = {
                "ok": True,
                "task_id": persisted_job_id,
                "queued": persisted_status == "pending",
                "queue_position": (
                    organize_operation_queue_position(persisted_job_id)
                    if persisted_status == "pending"
                    else 0
                ),
                "replayed": True,
            }
        else:
            raise AgentToolError(
                "光鸭操作未能可靠入队，请重新生成预览后重试",
                code="precondition_failed",
            )
    consumed = _consume(flow)
    internal_id = str(task.get("task_id") or "")
    operation_ref = (
        organize_operation_public_ref(internal_id)
        if re.fullmatch(r"[0-9a-f]{32}", internal_id)
        else ""
    )
    queued = bool(task.get("queued"))
    return ToolResult(
        True,
        "accepted",
        "光鸭文件变更已排队" if queued else "光鸭文件变更任务已启动",
        data={
            "queued": queued,
            "queue_position": max(0, int(task.get("queue_position") or 0)),
            "replayed": bool(task.get("replayed")),
            "operation_ref": operation_ref,
            **flow.preview_safe,
            "requires_manual": not consumed,
        },
        evidence=[
            Evidence(
                "organize_queue",
                "冻结计划已提交到可恢复的光鸭写入队列；执行结果只公开聚合计数。",
                _now(),
            )
        ],
        suggestions=[
            "可以稍后查询光鸭整理状态查看完成、部分完成或失败数量。",
            *(["会话状态未能可靠消费，请勿重复提交同一计划。"] if not consumed else []),
        ],
    )


def execute_durable_guangya_fs_change_job(
    payload: dict[str, Any], *, cancel_check=None
) -> dict[str, Any]:
    return execute_fs_change_plan(payload, cancel_check=cancel_check)
