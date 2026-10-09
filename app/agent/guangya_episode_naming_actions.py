"""Agent 光鸭剧集分季命名：紧凑映射、确定性展开、统一确认执行。"""

from __future__ import annotations

from typing import Any

from app.agent.errors import AgentToolError
from app.agent.guangya_fs_change_actions import (
    guangya_fs_change_preview_arguments,
    prepare_guangya_fs_change_confirmation,
    preview_guangya_fs_change,
)
from app.agent.models import ToolContext, ToolResult, ToolReference
from app.clients.guangya import GuangYaClient
from app.modules.guangya_episode_naming import (
    GuangYaEpisodeNamingError,
    compile_episode_naming_operations,
    summarize_episode_naming_observation,
)
from app.modules.guangya_workspace import (
    GuangYaWorkspaceError,
    create_directory_observation,
    discard_observation,
    observation_ref,
)


def _integer(value: object, *, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise AgentToolError(f"{field} 必须在 {minimum} 到 {maximum} 之间")
    return int(value)


def _path(value: object, *, field: str, allow_empty: bool = False) -> str:
    path = str(value or "").strip().replace("\\", "/")
    if allow_empty and not path:
        return ""
    if not path:
        raise AgentToolError(f"{field} 不能为空")
    if not path.startswith("/"):
        path = "/" + path
    parts = [part for part in path.split("/") if part]
    if len(path) > 2048 or any(part in {".", ".."} for part in parts):
        raise AgentToolError(f"{field} 必须是精确光鸭绝对路径")
    return "/" + "/".join(parts) if parts else "/"


def guangya_episode_naming_inspect_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("光鸭剧集命名盘点参数必须是对象")
    if set(arguments) - {"target_root"}:
        raise AgentToolError("光鸭剧集命名盘点包含不支持的参数")
    return {"target_root": _path(arguments.get("target_root"), field="target_root")}


def _fresh_observation(*, target_root: str, owner: str) -> dict[str, Any]:
    client: GuangYaClient | None = None
    try:
        client = GuangYaClient()
        if not client.logged_in:
            raise AgentToolError("光鸭账号尚未连接", code="precondition_failed")
        return create_directory_observation(
            client,
            owner=owner,
            path=target_root,
            recursive=True,
            max_items=2000,
            max_depth=12,
            operation="tree",
        )
    except AgentToolError:
        raise
    except GuangYaWorkspaceError as exc:
        raise AgentToolError(str(exc), code="precondition_failed") from exc
    finally:
        if client is not None:
            client.close()


def inspect_guangya_episode_naming(
    arguments: dict[str, Any], context: ToolContext
) -> ToolResult:
    """一次扫描完整作品目录，只返回供季集映射使用的紧凑盘点。"""
    if not context.owner:
        raise AgentToolError("光鸭剧集命名盘点需要已登录会话", code="precondition_failed")
    observation = _fresh_observation(
        target_root=str(arguments["target_root"]), owner=context.owner
    )
    try:
        data = summarize_episode_naming_observation(
            observation, target_root=str(arguments["target_root"])
        )
    except GuangYaEpisodeNamingError as exc:
        raise AgentToolError(str(exc), code="precondition_failed") from exc
    finally:
        discard_observation(str(observation.get("plan_id") or ""))
    return ToolResult(
        ok=True,
        status="success",
        summary=(
            f"剧集命名盘点完成：{int(data['video_count'])} 个视频、"
            f"{int(data['subtitle_count'])} 个字幕，{int(data['source_group_count'])} 个来源目录；"
            f"{int(data['unmatched_subtitle_count'])} 个字幕未唯一匹配"
        ),
        data=data,
        model_data={
            **{key: value for key, value in data.items() if key not in {"target_root", "groups"}},
            "groups": [
                {**{key: value for key, value in group.items() if key != "source_path"},
                 "source_group": index,
                 "relative_components": group["source_path"].removeprefix(data["target_root"]).strip("/").split("/")}
                for index, group in enumerate(data["groups"], start=1)
            ],
        },
        references=[ToolReference("episode_naming_scope", {
            "target_root": data["target_root"],
            "source_paths": [group["source_path"] for group in data["groups"]],
        })],
        suggestions=[
            "先核对正片、extras/unknown及字幕配对；未匹配字幕不代表已处理，源季号/本地数量不能证明TMDB偏移。",
            "只在取得可靠 TMDB 映射或用户明确指定映射后建卡；缺依据则说明待核对，不能猜测。",
        ],
    )


def guangya_episode_naming_plan_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise AgentToolError("光鸭剧集命名方案参数必须是对象")
    allowed = {"title", "episode_naming_scope_ref", "groups", "trigger_strm"}
    if set(arguments) - allowed:
        raise AgentToolError("光鸭剧集命名方案包含不支持的参数")
    title = str(arguments.get("title") or "").strip()
    if not 1 <= len(title) <= 180:
        raise AgentToolError("title 长度必须在 1 到 180 之间")
    scope_ref = arguments.get("episode_naming_scope_ref")
    if not isinstance(scope_ref, str) or not scope_ref.startswith("ref_") or len(scope_ref) > 200:
        raise AgentToolError("请先盘点目录，并原样传入 episode_naming_scope_ref")
    raw_groups = arguments.get("groups")
    if not isinstance(raw_groups, list) or not 1 <= len(raw_groups) <= 32:
        raise AgentToolError("groups 必须包含 1 到 32 个篇章映射")
    groups: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_groups, start=1):
        if not isinstance(raw, dict):
            raise AgentToolError(f"第 {index} 个篇章映射必须是对象")
        expected = {
            "source_group",
            "target_season",
            "source_episode_start",
            "source_episode_end",
            "target_episode_start",
            "source_season",
            "name_contains",
            "expected_count",
            "include_extras",
        }
        if set(raw) - expected:
            raise AgentToolError(f"第 {index} 个篇章映射包含不支持的参数")
        if type(raw.get("include_extras", False)) is not bool:
            raise AgentToolError("include_extras 必须是布尔值")
        required = {
            "target_season",
            "source_episode_start",
            "source_episode_end",
            "expected_count",
        }
        if not required.issubset(raw):
            raise AgentToolError(f"第 {index} 个篇章映射缺少必要参数")
        source_start = _integer(
            raw.get("source_episode_start"), field="source_episode_start", minimum=0, maximum=9999
        )
        source_end = _integer(
            raw.get("source_episode_end"), field="source_episode_end", minimum=0, maximum=9999
        )
        if source_end < source_start:
            raise AgentToolError("source_episode_end 不能小于 source_episode_start")
        group: dict[str, Any] = {
            "source_group": _integer(raw.get("source_group"), field="source_group", minimum=1, maximum=2000),
            "target_season": _integer(
                raw.get("target_season"), field="target_season", minimum=0, maximum=999
            ),
            "source_episode_start": source_start,
            "source_episode_end": source_end,
            "target_episode_start": _integer(
                raw.get("target_episode_start", 1),
                field="target_episode_start",
                minimum=1,
                maximum=9999,
            ),
        }
        if raw.get("source_season") is not None:
            group["source_season"] = _integer(
                raw.get("source_season"), field="source_season", minimum=0, maximum=999
            )
        name_contains = str(raw.get("name_contains") or "").strip()
        if name_contains:
            if len(name_contains) > 160:
                raise AgentToolError("name_contains 长度不能超过 160")
            group["name_contains"] = name_contains
        if raw.get("expected_count") is not None:
            group["expected_count"] = _integer(
                raw.get("expected_count"), field="expected_count", minimum=1, maximum=200
            )
        group["include_extras"] = raw.get(
            "include_extras", group.get("source_season") == 0 or group["target_season"] == 0
        )
        groups.append(group)
    trigger_strm = arguments.get("trigger_strm", True)
    if type(trigger_strm) is not bool:
        raise AgentToolError("trigger_strm 必须是布尔值")
    return {
        "title": title,
        "episode_naming_scope_ref": scope_ref,
        "groups": groups,
        "trigger_strm": trigger_strm,
    }


def _compile(arguments: dict[str, Any], context: ToolContext) -> tuple[dict[str, Any], dict[str, Any]]:
    if not context.owner:
        raise AgentToolError("光鸭剧集命名需要已登录会话", code="precondition_failed")
    scope = arguments["episode_naming_scope"]
    target_root = str(scope["target_root"])
    groups = []
    for requested in arguments["groups"]:
        index = requested["source_group"]
        if not 1 <= index <= len(scope["source_paths"]):
            raise AgentToolError("来源目录编号不在本次盘点中，请重新盘点", code="precondition_failed")
        groups.append({**{key: value for key, value in requested.items() if key != "source_group"},
                       "source_path": scope["source_paths"][index - 1]})
    # 引用只绑定范围；写前仍刷新真实目录，冻结最新文件而非重放旧快照。
    observation = _fresh_observation(target_root=target_root, owner=context.owner)
    try:
        compiled = compile_episode_naming_operations(
            observation,
            title=str(arguments["title"]),
            target_root=target_root,
            groups=groups,
        )
        fs_arguments = guangya_fs_change_preview_arguments(
            {
                "observation_ref": observation_ref(str(observation["plan_id"])),
                "operations": compiled["operations"],
                "trigger_strm": bool(arguments["trigger_strm"]),
            }
        )
    except GuangYaEpisodeNamingError as exc:
        discard_observation(str(observation.get("plan_id") or ""))
        raise AgentToolError(str(exc), code="precondition_failed") from exc
    except Exception:
        discard_observation(str(observation.get("plan_id") or ""))
        raise
    return fs_arguments, compiled


def prepare_guangya_episode_naming_confirmation(
    arguments: dict[str, Any], context: ToolContext
) -> tuple[ToolResult, str]:
    """编译紧凑映射并直接生成统一 fs-change 人工确认卡。"""
    fs_arguments, compiled = _compile(arguments, context)
    try:
        preview = preview_guangya_fs_change(fs_arguments, context)
        confirmation, fingerprint = prepare_guangya_fs_change_confirmation({}, context)
    finally:
        discard_observation(str(fs_arguments.get("observation_ref") or ""))
    mapping = {
        **{key: int(compiled[key]) for key in (
            "selected_files", "video_count", "subtitle_count", "unmatched_subtitle_count", "unselected_subtitle_count",
            "included_extra_count", "created_directories", "skipped_noop",
        )},
        "subtitle_skips": list(compiled["subtitle_skips"]),
        "groups": list(compiled["groups"]),
    }
    confirmation.summary = (
        f"确认后将按分季方案执行 {int(preview.data.get('total') or 0)} 项光鸭文件变更"
        f"（{mapping['video_count']} 个视频、{mapping['subtitle_count']} 个配套字幕）"
    )
    if mapping["included_extra_count"]:
        confirmation.summary += f"（含 {mapping['included_extra_count']} 个非正片，需明确授权纳入）"
    if mapping["unmatched_subtitle_count"]:
        confirmation.summary += f"；另有 {mapping['unmatched_subtitle_count']} 个字幕未唯一匹配，不在本计划内"
    if mapping["unselected_subtitle_count"]:
        confirmation.summary += f"；{mapping['unselected_subtitle_count']} 个字幕对应视频未纳入本次映射，保持不动"
    confirmation.data = {**confirmation.data, "episode_naming": mapping}
    confirmation.model_data = dict(confirmation.data)
    confirmation.suggestions = [
        "请核对视频/字幕数量、未处理字幕和非正片纳入范围；一个持久任务只执行这张卡内的计划。",
        "源目录/已观察集数不能证明 TMDB 偏移；映射未核实时不要确认。",
    ]
    return confirmation, fingerprint
