"""Web 已有配置的 Agent 纵切；复用业务仓储，不开放任意配置或凭据写入。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app import database as db
from app.agent.errors import AgentToolError
from app.agent.local_media_source_actions import _snapshot as source_snapshot
from app.agent.models import ToolResult
from app.agent.public_safety import sanitize_public_text
from app.logger import get_logger
from app.modules import recognition_knowledge as knowledge
from app.modules.local_media_scheduler import get_local_media_scheduler
from app.modules.local_path_mapping import (
    assert_within,
    require_container_absolute_path,
    validate_source_target_roots,
)

logger = get_logger(__name__)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()


def _object(
    arguments: Any, allowed: set[str], required: set[str] = frozenset()
) -> dict[str, Any]:
    if (
        not isinstance(arguments, dict)
        or set(arguments) - allowed
        or not required <= set(arguments)
    ):
        raise AgentToolError("配置参数缺失或包含禁止的字段")
    return dict(arguments)


def _number(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= 2_147_483_647
    ):
        raise AgentToolError("配置条目编号必须是正整数")
    return value


def _text(value: Any, limit: int = 160, *, empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or len(value) > limit
        or "\x00" in value
        or (not empty and not value.strip())
    ):
        raise AgentToolError("配置文字内容无效")
    return value.strip()


def knowledge_list_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    args = _object(arguments, {"keyword", "knowledge_type"})
    if "keyword" in args:
        args["keyword"] = _text(args["keyword"], empty=True)
    if args.get("knowledge_type", "") not in {"", "release_group", "release_suffix"}:
        raise AgentToolError("knowledge_type 无效")
    return args


def _knowledge_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "entry_number": int(row["id"]),
        "knowledge_type": row["knowledge_type"],
        "canonical_value": sanitize_public_text(row["canonical_value"], limit=160),
        "aliases": [sanitize_public_text(value, limit=160) for value in row["aliases"]],
        "source": row["source"],
        "disabled": bool(row["disabled"]),
    }


def list_knowledge(arguments: dict[str, Any]) -> ToolResult:
    args = knowledge_list_arguments(arguments)
    payload = knowledge.list_entries(**args, limit=50)
    return ToolResult(
        True,
        "completed",
        "已读取识别知识配置",
        data={
            "items": [_knowledge_public(row) for row in payload["items"]],
            "summary": payload["summary"],
            "limit": 50,
        },
    )


def knowledge_mutation_arguments(
    arguments: dict[str, Any], operation: str
) -> dict[str, Any]:
    fields = {"knowledge_type", "canonical_value", "aliases", "disabled"}
    allowed = (
        {"entry_number"}
        if operation == "delete"
        else fields | ({"entry_number"} if operation == "update" else set())
    )
    required = (
        {"knowledge_type", "canonical_value"}
        if operation == "create"
        else {"entry_number"}
    )
    args = _object(arguments, allowed, required)
    if "entry_number" in args:
        args["entry_number"] = _number(args["entry_number"])
    if operation == "update" and len(args) == 1:
        raise AgentToolError("至少提供一个需要修改的识别知识字段")
    if "knowledge_type" in args and args["knowledge_type"] not in {
        "release_group",
        "release_suffix",
    }:
        raise AgentToolError("识别知识类型无效")
    if "canonical_value" in args:
        args["canonical_value"] = _text(args["canonical_value"])
    if "aliases" in args:
        if not isinstance(args["aliases"], list) or len(args["aliases"]) > 24:
            raise AgentToolError("aliases 必须是最多24项的字符串数组")
        args["aliases"] = [_text(value) for value in args["aliases"]]
    if "disabled" in args and not isinstance(args["disabled"], bool):
        raise AgentToolError("disabled 必须是布尔值")
    return args


def _knowledge_current(args: dict[str, Any], operation: str) -> dict[str, Any]:
    if operation == "create":
        existing = knowledge.lookup_any(args["canonical_value"], args["knowledge_type"])
        if existing:
            raise AgentToolError(
                "该识别知识已存在，请修改现有条目", code="precondition_failed"
            )
        return {}
    current = knowledge.get_entry(args["entry_number"])
    if current is None:
        raise AgentToolError("识别知识不存在", code="precondition_failed")
    if operation == "delete" and current["source"] == "builtin":
        raise AgentToolError("内置知识不能删除，请将其停用", code="precondition_failed")
    return current


def prepare_knowledge(
    arguments: dict[str, Any], operation: str
) -> tuple[ToolResult, str]:
    args = knowledge_mutation_arguments(arguments, operation)
    current = _knowledge_current(args, operation)
    fields = {key: value for key, value in args.items() if key != "entry_number"}
    try:
        if operation != "delete":
            normalized = knowledge._validate_values(fields, existing=current)
            with db.get_conn() as conn:
                knowledge._assert_no_alias_collision(
                    normalized["knowledge_type"],
                    normalized["aliases"],
                    conn=conn,
                    exclude_id=current.get("id"),
                )
    except ValueError as exc:
        raise AgentToolError(str(exc), code="precondition_failed") from exc
    frozen = {
        "operation": operation,
        "arguments": args,
        "snapshot": _fingerprint(current),
    }
    return ToolResult(
        True,
        "confirmation_required",
        {
            "create": "确认新增识别知识",
            "update": "确认修改识别知识",
            "delete": "确认删除识别知识",
        }[operation],
        data={
            "operation": operation,
            "before": _knowledge_public(current) if current else None,
            "changes": fields,
            "effects": ["只修改识别知识，不立即扫描、移动或重命名任何媒体文件。"],
        },
    ), json.dumps(frozen)


def execute_knowledge(
    arguments: dict[str, Any], token: str, operation: str
) -> ToolResult:
    args = knowledge_mutation_arguments(arguments, operation)
    current = _knowledge_current(args, operation)
    try:
        frozen = json.loads(token)
    except (ValueError, TypeError) as exc:
        raise AgentToolError("确认计划无效", code="confirmation_invalid") from exc
    if frozen != {
        "operation": operation,
        "arguments": args,
        "snapshot": _fingerprint(current),
    }:
        raise AgentToolError("识别知识已变化，请重新预检", code="precondition_failed")
    fields = {key: value for key, value in args.items() if key != "entry_number"}
    try:
        if operation == "delete":
            ok = (
                knowledge.delete_entry(args["entry_number"])
                and knowledge.get_entry(args["entry_number"]) is None
            )
            row = None
        else:
            row = (
                knowledge.create_entry({**fields, "source": "user"})
                if operation == "create"
                else knowledge.update_entry(args["entry_number"], fields)
            )
            row = knowledge.get_entry(int(row["id"]))
            ok = row is not None
    except ValueError as exc:
        raise AgentToolError(str(exc), code="precondition_failed") from exc
    return ToolResult(
        ok,
        "completed" if ok else "partial",
        "识别知识已更新并回读核对" if ok else "识别知识写后核对失败",
        data={"operation": operation, "item": _knowledge_public(row) if row else None},
    )


_SOURCE_FIELDS = {
    "name",
    "local_root",
    "qb_path_prefix",
    "enabled",
    "media_type",
    "mode",
}


def source_mutation_arguments(
    arguments: dict[str, Any], operation: str
) -> dict[str, Any]:
    allowed = (
        {"source_number"}
        if operation == "delete"
        else _SOURCE_FIELDS | ({"source_number"} if operation == "update" else set())
    )
    required = {"name", "local_root"} if operation == "create" else {"source_number"}
    args = _object(arguments, allowed, required)
    if "source_number" in args:
        args["source_number"] = _number(args["source_number"])
    if operation == "update" and len(args) == 1:
        raise AgentToolError("至少提供一个要修改的来源字段")
    for key, maximum in (("name", 128), ("local_root", 2048), ("qb_path_prefix", 2048)):
        if key in args:
            args[key] = _text(args[key], maximum, empty=key == "qb_path_prefix")
    if "enabled" in args and not isinstance(args["enabled"], bool):
        raise AgentToolError("enabled 必须是布尔值")
    if args.get("mode", "move") not in {"move", "preview_only"}:
        raise AgentToolError("mode 仅支持 move 或 preview_only")
    if args.get("media_type", "auto") not in {"auto", "movie", "tv", "nsfw"}:
        raise AgentToolError("media_type 无效")
    return args


def _source_current(args: dict[str, Any], operation: str) -> dict[str, Any]:
    with db.get_conn() as conn:
        if operation == "create":
            rows = conn.execute(
                "SELECT id,name,local_root FROM local_media_sources WHERE owner='admin' ORDER BY id"
            ).fetchall()
            return {"existing": [dict(row) for row in rows]}
        return source_snapshot(conn, args["source_number"])


def _source_payload(
    args: dict[str, Any], snapshot: dict[str, Any], operation: str
) -> dict[str, Any]:
    if operation == "delete":
        return {}
    current = snapshot.get("source", {})
    payload = {
        "name": current.get("name", ""),
        "local_root": current.get("local_root", ""),
        "qb_profile": current.get("qb_profile", "configured:qb"),
        "qb_path_prefix": current.get("qb_path_prefix", ""),
        "enabled": bool(current.get("enabled", False)),
        "media_type": current.get("media_type", "auto"),
        "mode": current.get("mode", "preview_only"),
    }
    payload.update({key: value for key, value in args.items() if key in _SOURCE_FIELDS})
    try:
        declared = require_container_absolute_path(payload["local_root"])
        directory = assert_within(declared, declared)
        if not directory.is_dir() or directory == Path(directory.anchor):
            raise ValueError("来源必须是已存在的非根目录")
        targets = [Path(item["path"]) for item in snapshot.get("targets", [])]
        validate_source_target_roots(directory, targets)
    except (ValueError, OSError) as exc:
        raise AgentToolError(
            "本地来源目录不可用或与归档目录重叠，请核对目录", code="precondition_failed"
        ) from exc
    payload["local_root"] = str(directory)
    # targets=None 由仓储保留现有映射，新来源不自动创建任何媒体库映射。
    payload["targets"] = None
    if operation == "update":
        payload["source_id"] = int(current["id"])
    else:
        if any(
            row["local_root"] == str(directory) or row["name"] == payload["name"]
            for row in snapshot["existing"]
        ):
            raise AgentToolError(
                "相同名称或目录的媒体来源已存在", code="precondition_failed"
            )
    return payload


def prepare_source(arguments: dict[str, Any], operation: str) -> tuple[ToolResult, str]:
    args = source_mutation_arguments(arguments, operation)
    current = _source_current(args, operation)
    payload = _source_payload(args, current, operation)
    frozen = {
        "operation": operation,
        "arguments": args,
        "snapshot": _fingerprint(current),
        "payload": payload,
    }
    return ToolResult(
        True,
        "confirmation_required",
        {
            "create": "确认新增本地媒体来源",
            "update": "确认修改本地媒体来源",
            "delete": "确认删除本地媒体来源",
        }[operation],
        data={
            "operation": operation,
            "source_number": args.get("source_number"),
            "name": sanitize_public_text(
                payload.get("name", current.get("source", {}).get("name", "")),
                limit=128,
            ),
            "changed_fields": sorted(key for key in args if key != "source_number"),
            "enabled": payload.get("enabled"),
            "mode": payload.get("mode"),
            "effects": [
                "只修改来源配置，不移动或删除媒体文件；已有媒体库映射保持不变。",
                "开启 qB 接管后，后续下载完成事件可触发已有整理流程。",
            ],
        },
    ), json.dumps(frozen)


def execute_source(arguments: dict[str, Any], token: str, operation: str) -> ToolResult:
    args = source_mutation_arguments(arguments, operation)
    current = _source_current(args, operation)
    payload = _source_payload(args, current, operation)
    try:
        frozen = json.loads(token)
    except (ValueError, TypeError) as exc:
        raise AgentToolError("确认上下文无效", code="confirmation_invalid") from exc
    if frozen != {
        "operation": operation,
        "arguments": args,
        "snapshot": _fingerprint(current),
        "payload": payload,
    }:
        raise AgentToolError("来源配置已变化，请重新预检", code="precondition_failed")
    try:
        if operation == "delete":
            source_id = current["source"]["id"]
            ok = (
                db.delete_local_media_source(source_id, owner="admin")
                and db.get_local_media_source(source_id, owner="admin") is None
            )
        else:
            source_id = db.save_local_media_source_bundle(**payload, owner="admin")
            row = db.get_local_media_source(source_id, owner="admin")
            ok = row is not None and all(
                getattr(row, key) == payload[key] for key in _SOURCE_FIELDS
            )
    except ValueError as exc:
        raise AgentToolError(str(exc), code="precondition_failed") from exc
    try:
        get_local_media_scheduler().reload()
        refreshed = True
    except Exception as exc:
        logger.exception("本地来源运行时刷新失败 type=%s", type(exc).__name__)
        refreshed = False
    return ToolResult(
        ok,
        "completed" if ok and refreshed else "partial",
        "本地媒体来源配置已保存" if operation != "delete" else "本地媒体来源已删除",
        data={
            "operation": operation,
            "verified": ok,
            "runtime_refreshed": refreshed,
            "requires_mapping": operation == "create",
        },
    )


_MAPPING_KEYS = {"jellyfin": "JELLYFIN_PATH_MAPPINGS", "emby": "EMBY_PATH_MAPPINGS"}


def mapping_arguments(
    arguments: dict[str, Any], operation: str = "list"
) -> dict[str, Any]:
    allowed = {"provider"}
    required = {"provider"}
    if operation in {"update", "delete"}:
        allowed.add("mapping_number")
        required.add("mapping_number")
    if operation in {"create", "update"}:
        allowed |= {"local_path", "server_path"}
        if operation == "create":
            required |= {"local_path", "server_path"}
    args = _object(arguments, allowed, required)
    if args["provider"] not in _MAPPING_KEYS:
        raise AgentToolError("provider 仅支持 jellyfin 或 emby")
    if "mapping_number" in args:
        args["mapping_number"] = _number(args["mapping_number"])
    for key in ("local_path", "server_path"):
        if key in args:
            args[key] = _text(args[key], 2048)
    if operation == "update" and not ({"local_path", "server_path"} & args.keys()):
        raise AgentToolError("至少提供一个需要修改的映射路径")
    return args


def _mapping_items(provider: str) -> list[dict[str, str]]:
    from app import config
    from app.modules.media_server_path_mapping import parse_media_server_path_mappings

    try:
        return [
            {"local": item.local_prefix, "server": item.server_prefix}
            for item in parse_media_server_path_mappings(
                config.get(_MAPPING_KEYS[provider], "")
            )
        ]
    except ValueError as exc:
        raise AgentToolError(
            "现有媒体库路径映射无效，请先在Web修复", code="precondition_failed"
        ) from exc


def _mapping_public(items: list[dict[str, str]]) -> list[dict[str, Any]]:
    return [
        {
            "mapping_number": index,
            "local_directory": sanitize_public_text(
                item["local"].rsplit("/", 1)[-1], limit=160
            ),
            "server_directory": sanitize_public_text(
                item["server"].rsplit("/", 1)[-1], limit=160
            ),
        }
        for index, item in enumerate(items, 1)
    ]


def list_path_mappings(arguments: dict[str, Any]) -> ToolResult:
    from app import config

    args = mapping_arguments(arguments)
    items = _mapping_items(args["provider"])
    return ToolResult(
        True,
        "completed",
        "已读取媒体服务器路径映射",
        data={
            "provider": args["provider"],
            "items": _mapping_public(items),
            "count": len(items),
            "managed_by_environment": config.has_external_override(
                _MAPPING_KEYS[args["provider"]]
            ),
            "scope": "STRM/本地路径到媒体服务器可见路径的前缀映射，不是本地分类归档绑定。",
        },
    )


def _mapping_plan(
    args: dict[str, Any], operation: str
) -> tuple[dict[str, Any], bytes | None]:
    from app import config
    from app.modules.media_server_path_mapping import encode_media_server_path_mappings

    key = _MAPPING_KEYS[args["provider"]]
    if config.has_external_override(key):
        raise AgentToolError(
            "路径映射由部署环境管理，不能从Agent覆盖", code="precondition_failed"
        )
    raw, _ = config.read_env_snapshot(config.ENV_FILE)
    current = _mapping_items(args["provider"])
    modified = [dict(item) for item in current]
    if operation == "create":
        modified.append({"local": args["local_path"], "server": args["server_path"]})
    else:
        index = args["mapping_number"] - 1
        if index >= len(modified):
            raise AgentToolError("路径映射不存在", code="precondition_failed")
        if operation == "delete":
            modified.pop(index)
        else:
            modified[index].update(
                {
                    mapped: args[source]
                    for source, mapped in (
                        ("local_path", "local"),
                        ("server_path", "server"),
                    )
                    if source in args
                }
            )
    try:
        encoded = encode_media_server_path_mappings(json.dumps(modified))
    except ValueError as exc:
        raise AgentToolError(str(exc), code="precondition_failed") from exc
    return {
        "operation": operation,
        "arguments": args,
        "key": key,
        "encoded": encoded,
        "before": current,
        "snapshot": hashlib.sha256(raw or b"").hexdigest(),
    }, raw


def prepare_path_mapping(
    arguments: dict[str, Any], operation: str
) -> tuple[ToolResult, str]:
    args = mapping_arguments(arguments, operation)
    plan, _ = _mapping_plan(args, operation)
    return ToolResult(
        True,
        "confirmation_required",
        "确认修改媒体库路径映射",
        data={
            "operation": operation,
            "provider": args["provider"],
            "mapping_number": args.get("mapping_number"),
            "before": _mapping_public(plan["before"]),
            "effects": [
                "只修改本服务器路径前缀映射，不重写STRM文件、不修改其它媒体服务器配置。",
                "后续媒体库刷新会使用新映射；不自动开启全局刷新回退。",
            ],
        },
    ), _fingerprint(plan)


def execute_path_mapping(
    arguments: dict[str, Any], token: str, operation: str
) -> ToolResult:
    from app import config
    from app.modules.process_lock import CrossProcessLock

    args = mapping_arguments(arguments, operation)
    lock = CrossProcessLock("media-library-mappings")
    if not lock.acquire():
        raise AgentToolError("媒体库映射正在被其它请求保存", code="precondition_failed")
    try:
        plan, raw = _mapping_plan(args, operation)
        if _fingerprint(plan) != token:
            raise AgentToolError(
                "路径映射配置已变化，请重新预检", code="precondition_failed"
            )
        try:
            config.update_runtime_env_file(
                config.ENV_FILE, {plan["key"]: plan["encoded"]}, expected=raw
            )
        except (
            config.ConcurrentConfigUpdateError,
            config.ExternalConfigOverrideError,
        ) as exc:
            raise AgentToolError(
                "路径映射被其它设置更新，请重新预检", code="precondition_failed"
            ) from exc
        except (OSError, ValueError) as exc:
            raise AgentToolError(
                "路径映射未能保存，请检查配置文件状态", code="unavailable"
            ) from exc
        ok = config.get(plan["key"], "") == plan["encoded"]
        items = _mapping_items(args["provider"])
        return ToolResult(
            ok,
            "completed" if ok else "partial",
            "媒体库路径映射已保存并回读核对" if ok else "路径映射回读不一致",
            data={
                "operation": operation,
                "provider": args["provider"],
                "items": _mapping_public(items),
                "count": len(items),
            },
        )
    finally:
        lock.release()
