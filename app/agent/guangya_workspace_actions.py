"""Agent 光鸭目录观察：安全文件名分页与最近观察上下文。"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.agent.errors import AgentToolError
from app.agent.models import Evidence, ToolContext, ToolResult
from app.agent.session_context import (
    AgentContextWriteGuard,
    AgentSessionContextRepository,
)
from app.clients.guangya import GuangYaClient
from app.modules.guangya_workspace import (
    GuangYaWorkspaceError,
    GuangYaWorkspaceStale,
    create_directory_observation,
    create_multi_directory_observation,
    create_path_observation,
    discard_observation,
    discard_owner_observations,
    load_directory_observation,
    observation_page,
    valid_observation_ref,
)

logger = logging.getLogger(__name__)
_CONTEXT_TYPE = "guangya_workspace"
_TTL_SECONDS = 10 * 60.0
_MAX_PAGE_SIZE = 50


@dataclass
class _Flow:
    owner: str
    observation_ref: str
    page: int = 1
    page_size: int = _MAX_PAGE_SIZE
    has_more: bool | None = None
    generation: int = 0
    revision: int = 0
    updated_at: float = 0.0


_lock = threading.RLock()
_flows: dict[str, _Flow] = {}
_repository: AgentSessionContextRepository | None = None


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def configure_guangya_workspace_context(
    repository: AgentSessionContextRepository | None,
) -> None:
    global _repository
    _repository = repository


def reset_guangya_workspace_context_for_tests() -> None:
    global _repository
    with _lock:
        _flows.clear()
    _repository = None


def clear_guangya_workspace_context(*, owner: str) -> int:
    """清除 owner 的缓存与全部短期观察引用。"""
    owner_key = str(owner or "").strip()
    if not owner_key:
        return 0
    with _lock:
        _flows.pop(owner_key, None)
    return discard_owner_observations(owner_key)


def _begin(owner: str) -> AgentContextWriteGuard:
    if _repository is None:
        return AgentContextWriteGuard(0, 0)
    begin = getattr(_repository, "begin_context", None)
    if not callable(begin):
        return AgentContextWriteGuard(0, 0)
    return begin(owner=owner, context_type=_CONTEXT_TYPE)


def _begin_update(owner: str) -> tuple[str, AgentContextWriteGuard]:
    if _repository is not None:
        begin_update = getattr(_repository, "begin_context_update", None)
        if callable(begin_update):
            persisted, guard = begin_update(
                owner=owner,
                context_type=_CONTEXT_TYPE,
            )
            ref = ""
            if persisted is not None:
                ref = (
                    str(persisted.payload.get("observation_ref") or "").strip().upper()
                )
                if not valid_observation_ref(ref):
                    ref = ""
            if ref:
                page = _safe_cursor_integer(
                    persisted.payload.get("page"), default=1, maximum=200
                )
                page_size = _safe_cursor_integer(
                    persisted.payload.get("page_size"),
                    default=_MAX_PAGE_SIZE,
                    maximum=_MAX_PAGE_SIZE,
                )
                raw_has_more = persisted.payload.get("has_more")
                with _lock:
                    _flows[owner] = _Flow(
                        owner=owner,
                        observation_ref=ref,
                        page=page,
                        page_size=page_size,
                        has_more=(
                            raw_has_more if isinstance(raw_has_more, bool) else None
                        ),
                        generation=persisted.generation,
                        revision=persisted.revision,
                        updated_at=time.monotonic(),
                    )
            return ref, guard
    return latest_guangya_observation_ref(owner), _begin(owner)


def _save(flow: _Flow) -> bool:
    flow.updated_at = time.monotonic()
    payload = {
        "observation_ref": flow.observation_ref,
        "page": flow.page,
        "page_size": flow.page_size,
        "has_more": flow.has_more,
    }
    if _repository is not None:
        try:
            guarded = getattr(_repository, "replace_latest_guarded", None)
            if callable(guarded):
                if flow.generation <= 0:
                    return False
                persisted = guarded(
                    owner=flow.owner,
                    context_type=_CONTEXT_TYPE,
                    payload=payload,
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
                    payload=payload,
                    expires_at=time.time() + _TTL_SECONDS,
                )
        except Exception as exc:  # noqa: BLE001 - 上下文仓故障必须安全降级
            logger.warning("Agent 光鸭观察上下文保存失败 type=%s", type(exc).__name__)
            return False
    with _lock:
        _flows[flow.owner] = flow
    return True


def _safe_cursor_integer(value: Any, *, default: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if 1 <= number <= maximum else default


def latest_guangya_observation_cursor(owner: str) -> dict[str, Any]:
    """返回最近只读观察的分页游标；旧上下文缺字段时保持可兼容。"""
    if not owner:
        return {}
    if _repository is not None:
        try:
            persisted = _repository.get_latest(
                owner=owner, context_type=_CONTEXT_TYPE, now=time.time()
            )
            if persisted is not None:
                ref = (
                    str(persisted.payload.get("observation_ref") or "").strip().upper()
                )
                if valid_observation_ref(ref):
                    raw_has_more = persisted.payload.get("has_more")
                    return {
                        "observation_ref": ref,
                        "page": _safe_cursor_integer(
                            persisted.payload.get("page"), default=1, maximum=200
                        ),
                        "page_size": _safe_cursor_integer(
                            persisted.payload.get("page_size"),
                            default=_MAX_PAGE_SIZE,
                            maximum=_MAX_PAGE_SIZE,
                        ),
                        "has_more": (
                            raw_has_more if isinstance(raw_has_more, bool) else None
                        ),
                    }
        except Exception as exc:  # noqa: BLE001 - 上下文仓故障必须安全降级
            logger.warning("Agent 光鸭观察上下文读取失败 type=%s", type(exc).__name__)
        with _lock:
            _flows.pop(owner, None)
        return {}
    with _lock:
        flow = _flows.get(owner)
        if flow and time.monotonic() - flow.updated_at <= _TTL_SECONDS:
            return {
                "observation_ref": flow.observation_ref,
                "page": flow.page,
                "page_size": flow.page_size,
                "has_more": flow.has_more,
            }
        _flows.pop(owner, None)
    return {}


def latest_guangya_observation_ref(owner: str) -> str:
    return str(latest_guangya_observation_cursor(owner).get("observation_ref") or "")


def guangya_capabilities_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or arguments:
        raise AgentToolError("光鸭能力查询不接受参数")
    return {}


def summarize_guangya_capabilities(_arguments: dict[str, Any]) -> ToolResult:
    return ToolResult(
        True,
        "ok",
        "光鸭安全能力网关已启用：读取可直接执行，云端写入必须先冻结预览并由用户确认",
        data={
            "read_operations": ["list", "tree", "search", "stat"],
            "write_operations": ["rename", "move", "trash", "create_directory"],
            "write_policy": "preview_then_confirm",
            "trash_policy": "provider_recycle_bin",
            "opaque_references": True,
            "raw_sdk_exposed": False,
            "specialized_workflows": [
                "organize",
                "directory_scrape",
                "residual_cleanup",
                "media_hygiene",
                "schedule_and_status",
            ],
            "excluded_sensitive_capabilities": [
                "credential_management",
                "signed_download_url",
                "raw_provider_response",
                "permanent_delete",
            ],
        },
        evidence=[
            Evidence(
                "guangya_capability_policy",
                "能力清单只描述可安全公开的业务动作；不会返回登录凭据、对象 ID、签名直链或 SDK 原始响应。",
                _now(),
            )
        ],
        suggestions=[
            "可用 fs.query 按精确目录读取、递归查看或搜索对象。",
            "任何改名、移动、移入回收站或新建目录都必须先生成冻结计划。",
        ],
    )


def guangya_fs_query_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("光鸭文件查询参数必须是对象")
    allowed = {
        "operation",
        "path",
        "paths",
        "query",
        "kinds",
        "max_depth",
        "observation_ref",
        "page",
        "page_size",
        "max_items",
    }
    if set(arguments) - allowed:
        raise AgentToolError("光鸭文件查询包含不支持的参数")
    ref = str(arguments.get("observation_ref") or "").strip().upper()
    if ref:
        if not valid_observation_ref(ref):
            raise AgentToolError("observation_ref 格式无效")
        if set(arguments) - {"observation_ref", "page", "page_size"}:
            raise AgentToolError("继续分页时不能修改查询范围")
        result: dict[str, Any] = {"observation_ref": ref}
    else:
        operation = str(arguments.get("operation") or "list").strip().casefold()
        if operation not in {"list", "tree", "search", "stat"}:
            raise AgentToolError("operation 只能是 list、tree、search 或 stat")

        def normalize_path(value: object) -> str:
            path = str(value or "").strip().replace("\\", "/")
            if not path:
                raise AgentToolError("光鸭文件查询必须提供精确 path 或 paths")
            if not path.startswith("/"):
                path = "/" + path
            if len(path) > 2048 or any(
                part in {".", ".."} for part in path.split("/") if part
            ):
                raise AgentToolError("请提供精确的光鸭绝对路径")
            return path

        raw_paths = arguments.get("paths")
        if raw_paths is not None and "path" in arguments:
            raise AgentToolError("path 与 paths 不能同时提供")
        if raw_paths is not None:
            if not isinstance(raw_paths, list) or not 1 <= len(raw_paths) <= 32:
                raise AgentToolError("paths 必须包含 1 到 32 个精确目录")
            paths = tuple(dict.fromkeys(normalize_path(item) for item in raw_paths))
            if operation == "stat":
                raise AgentToolError("stat 操作只接受单个 path")
        else:
            paths = (normalize_path(arguments.get("path")),)
        query = str(arguments.get("query") or "").strip()
        if len(query) > 160:
            raise AgentToolError("query 最长 160 个字符")
        if query and operation in {"list", "tree"}:
            # Native tool callers often express recursive filtering as tree/list + query.
            # Normalize this equivalent shape instead of wasting an Agent round on syntax.
            operation = "search"
        if operation == "search" and not query:
            raise AgentToolError("search 操作必须提供 query")
        if operation == "stat" and query:
            raise AgentToolError("stat 操作不接受 query")
        raw_max_depth = arguments.get(
            "max_depth", 0 if operation in {"list", "stat"} else 12
        )
        if (
            isinstance(raw_max_depth, bool)
            or not isinstance(raw_max_depth, int)
            or not 0 <= raw_max_depth <= 12
        ):
            raise AgentToolError("max_depth 必须是 0 到 12 的整数")
        if operation == "stat" and "max_depth" in arguments:
            raise AgentToolError("stat 操作不接受 max_depth")
        raw_kinds = arguments.get("kinds", ())
        if raw_kinds is None:
            raw_kinds = ()
        if not isinstance(raw_kinds, (list, tuple)) or len(raw_kinds) > 6:
            raise AgentToolError("kinds 必须是最多 6 项的对象类型数组")
        allowed_kinds = {"directory", "video", "subtitle", "image", "metadata", "other"}
        kinds = tuple(
            dict.fromkeys(str(item or "").strip().casefold() for item in raw_kinds)
        )
        if any(not item or item not in allowed_kinds for item in kinds):
            raise AgentToolError("kinds 包含不支持的对象类型")
        if operation == "stat" and kinds:
            raise AgentToolError("stat 操作不接受 kinds")
        try:
            maximum = int(arguments.get("max_items", 500))
        except (TypeError, ValueError, OverflowError) as exc:
            raise AgentToolError("max_items 必须是整数") from exc
        if operation == "stat":
            if paths[0] == "/":
                raise AgentToolError("stat 操作不能以光鸭根目录为对象")
            if "max_items" in arguments:
                raise AgentToolError("stat 操作不接受 max_items")
            maximum = 1
        elif not 1 <= maximum <= 2000:
            raise AgentToolError("max_items 必须在 1 到 2000 之间")
        result = {
            "operation": operation,
            "path": paths[0],
            "paths": paths,
            "query": query,
            "kinds": kinds,
            "max_depth": raw_max_depth,
            "max_items": maximum,
        }
    try:
        page = int(arguments.get("page", 1))
        page_size = int(arguments.get("page_size", _MAX_PAGE_SIZE))
    except (TypeError, ValueError, OverflowError) as exc:
        raise AgentToolError("page 和 page_size 必须是整数") from exc
    if not 1 <= page <= 200 or not 1 <= page_size <= _MAX_PAGE_SIZE:
        raise AgentToolError(
            f"page 必须在 1 到 200，page_size 必须在 1 到 {_MAX_PAGE_SIZE}"
        )
    result.update(page=page, page_size=page_size)
    return result


def _public_error(exc: Exception) -> AgentToolError:
    if isinstance(exc, GuangYaWorkspaceStale):
        return AgentToolError(str(exc), code="precondition_failed")
    if isinstance(exc, GuangYaWorkspaceError):
        message = str(exc)
        if "路径不存在或名称不唯一" in message:
            message = "精确目录不存在或名称不唯一；请先列出父目录并使用返回的准确名称"
        elif "中间组件不是目录" in message:
            message = "目标路径的中间对象不是目录；请先列出父目录确认层级"
        return AgentToolError(message, code="precondition_failed")
    logger.warning("Agent 光鸭目录观察失败 type=%s", type(exc).__name__)
    return AgentToolError("光鸭目录观察当前不可用", code="unavailable")


def _read_observation_page(
    arguments: dict[str, Any],
    context: ToolContext,
) -> dict[str, Any]:
    if not context.owner:
        raise AgentToolError("光鸭目录观察需要已登录会话", code="precondition_failed")
    client: GuangYaClient | None = None
    new_observation = False
    _previous_ref, guard = _begin_update(context.owner)
    update_context = True
    requested_ref = str(arguments.get("observation_ref") or "").strip().upper()
    try:
        if requested_ref:
            payload = load_directory_observation(requested_ref, owner=context.owner)
        else:
            client = GuangYaClient()
            if not client.logged_in:
                raise AgentToolError("光鸭账号尚未连接", code="precondition_failed")
            if arguments["operation"] == "stat":
                payload = create_path_observation(
                    client,
                    owner=context.owner,
                    path=arguments["path"],
                )
            else:
                operation = str(arguments.get("operation") or "")
                paths = tuple(arguments.get("paths") or (arguments["path"],))
                creator = (
                    create_multi_directory_observation
                    if len(paths) > 1
                    else create_directory_observation
                )
                creator_arguments = {
                    "client": client,
                    "owner": context.owner,
                    "recursive": operation in {"tree", "search"},
                    "max_items": int(arguments["max_items"]),
                    "query": str(arguments.get("query") or ""),
                    "kinds": arguments.get("kinds") or (),
                    "max_depth": int(arguments.get("max_depth") or 0),
                    "operation": operation,
                }
                if len(paths) > 1:
                    creator_arguments["paths"] = paths
                else:
                    creator_arguments["path"] = paths[0]
                payload = creator(**creator_arguments)
            new_observation = True
        page = observation_page(
            payload, page=int(arguments["page"]), page_size=int(arguments["page_size"])
        )
    except AgentToolError:
        raise
    except Exception as exc:
        raise _public_error(exc) from exc
    finally:
        if client is not None:
            client.close()
    ref = str(page["observation_ref"])
    if update_context:
        flow = _Flow(
            owner=context.owner,
            observation_ref=ref,
            page=int(page["page"]),
            page_size=int(page["page_size"]),
            has_more=bool(page["has_more"]),
            generation=guard.generation,
            revision=guard.revision,
        )
        if not _save(flow):
            if new_observation:
                discard_observation(ref)
            raise AgentToolError(
                "目录观察已被更新请求取代，请重新读取",
                code="precondition_failed",
            )
        # ReAct 可能先观察共同父目录，再建立批量快照，并对个别对象做 stat。
        # 旧快照由模块容量/TTL 统一回收；这里若立即删除，模型后续使用先前
        # observation_ref 生成冻结计划时会无故失效。
    return page


def query_guangya_filesystem(
    arguments: dict[str, Any],
    context: ToolContext,
) -> ToolResult:
    page = _read_observation_page(arguments, context)
    count = len(page["entries"])
    operation = str(page.get("operation") or "list")
    labels = {"list": "列表", "tree": "递归观察", "search": "搜索", "stat": "对象详情"}
    scopes = tuple(str(item) for item in page.get("scopes") or ())
    scope_aliases = {name: f"S{index}" for index, name in enumerate(scopes, 1)}

    def compact_location(value: object) -> str:
        location = str(value or "")
        for name, alias in scope_aliases.items():
            if location == name:
                return alias
            prefix = name + " › "
            if location.startswith(prefix):
                return alias + " › " + location[len(prefix) :]
        return location

    model_data = {
        "observation_ref": page["observation_ref"],
        "scope": page["scope"],
        "scopes": [{"at": scope_aliases[name], "name": name} for name in scopes],
        "page": page["page"],
        "total": page["total"],
        "has_more": page["has_more"],
        "truncated": page["truncated"],
        "entries": [
            {
                "ref": item["object_ref"],
                "name": item["object_name"],
                "at": compact_location(item.get("location")),
            }
            for item in page["entries"]
        ],
    }
    return ToolResult(
        True,
        "found" if count else "empty",
        f"光鸭{labels.get(operation, '查询')}完成：第 {page['page']} 页展示 {count} 个对象",
        data=page,
        evidence=[
            Evidence(
                "guangya_filesystem_observation",
                "返回的是当前会话绑定的短时只读快照和不透明对象引用；不包含 Provider 对象 ID、绝对云端路径、凭据或签名 URL。",
                _now(),
            )
        ],
        suggestions=[
            *(
                ["还有更多对象，可以使用 observation_ref 继续分页。"]
                if page["has_more"]
                else []
            ),
            "如需改名、移动、移入回收站或新建目录，请先生成通用光鸭文件变更冻结预览。",
        ],
        model_data=model_data,
    )
