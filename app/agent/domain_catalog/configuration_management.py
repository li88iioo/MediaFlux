"""Web 已有识别知识和本地来源配置的原子工具。"""

from __future__ import annotations

from functools import partial

from app.agent.configuration_management_actions import (
    execute_knowledge,
    execute_path_mapping,
    execute_source,
    knowledge_list_arguments,
    knowledge_mutation_arguments,
    list_knowledge,
    list_path_mappings,
    mapping_arguments,
    prepare_knowledge,
    prepare_path_mapping,
    prepare_source,
    source_mutation_arguments,
)
from app.agent.models import RiskLevel, ToolSpec


def register_specs(registry, **_dependencies) -> None:
    registry.register(
        ToolSpec(
            name="config.recognition_knowledge",
            description="读取Web识别知识库的发布组/尾部制作组词条与别名，区分内置和用户词条；不是媒体TMDB锁定规则。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "maxLength": 160},
                    "knowledge_type": {
                        "type": "string",
                        "enum": ["", "release_group", "release_suffix"],
                    },
                },
                "additionalProperties": False,
            },
            validator=knowledge_list_arguments,
            handler=list_knowledge,
            domains=("config", "recognition"),
            related_tools=(
                "config.create_recognition_knowledge",
                "config.update_recognition_knowledge",
            ),
            examples=("看看发布组识别知识", "哪些发布组别名已经配置"),
        )
    )
    fields = {
        "knowledge_type": {
            "type": "string",
            "enum": ["release_group", "release_suffix"],
        },
        "canonical_value": {"type": "string", "minLength": 1, "maxLength": 160},
        "aliases": {
            "type": "array",
            "items": {"type": "string", "maxLength": 160},
            "maxItems": 24,
        },
        "disabled": {"type": "boolean"},
    }
    for operation in ("create", "update", "delete"):
        properties = {} if operation == "delete" else dict(fields)
        if operation != "create":
            properties["entry_number"] = {"type": "integer", "minimum": 1}
        registry.register(
            ToolSpec(
                name=f"config.{operation}_recognition_knowledge",
                description={
                    "create": "添加用户识别知识（发布组或尾部制作组与别名），与Web知识库共用实现。",
                    "update": "修改识别知识名称、别名或停用状态；不允许篡改来源、证据或内置知识身份。",
                    "delete": "删除用户识别知识；内置知识不能删除，只能停用。",
                }[operation],
                risk=RiskLevel.DANGER if operation == "delete" else RiskLevel.WRITE,
                requires_confirmation=True,
                parameters={
                    "type": "object",
                    "properties": properties,
                    "required": ["knowledge_type", "canonical_value"]
                    if operation == "create"
                    else ["entry_number"],
                    "additionalProperties": False,
                },
                validator=partial(knowledge_mutation_arguments, operation=operation),
                context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                    partial(prepare_knowledge, operation=operation)
                ),
                context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                    partial(execute_knowledge, operation=operation)
                ),
                domains=("config", "recognition"),
                related_tools=("config.recognition_knowledge",),
            )
        )
    fields = {
        "name": {"type": "string", "minLength": 1, "maxLength": 128},
        "local_root": {"type": "string", "minLength": 1, "maxLength": 2048},
        "qb_path_prefix": {"type": "string", "maxLength": 2048},
        "enabled": {"type": "boolean"},
        "media_type": {"type": "string", "enum": ["auto", "movie", "tv", "nsfw"]},
        "mode": {"type": "string", "enum": ["move", "preview_only"]},
    }
    for operation in ("create", "update", "delete"):
        properties = {} if operation == "delete" else dict(fields)
        if operation != "create":
            properties["source_number"] = {"type": "integer", "minimum": 1}
        registry.register(
            ToolSpec(
                name=f"config.{operation}_local_source",
                description={
                    "create": "新增Web本地媒体来源。必须使用用户明确提供的已存在容器目录；默认预览模式且关闭qB接管。媒体库路径映射需另行配置，不读取凭据或移动文件。",
                    "update": "修改本地媒体来源名称、容器目录、qB路径前缀、识别类型或整理模式，保留既有归档映射。source_number来自local_media.source_summaries。",
                    "delete": "删除本地媒体来源配置（不删除媒体文件）；有未完成任务的来源不能删除。",
                }[operation],
                risk=RiskLevel.DANGER if operation == "delete" else RiskLevel.WRITE,
                requires_confirmation=True,
                parameters={
                    "type": "object",
                    "properties": properties,
                    "required": ["name", "local_root"]
                    if operation == "create"
                    else ["source_number"],
                    "additionalProperties": False,
                },
                validator=partial(source_mutation_arguments, operation=operation),
                context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                    partial(prepare_source, operation=operation)
                ),
                context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                    partial(execute_source, operation=operation)
                ),
                domains=("config", "local_media"),
                related_tools=(
                    "local_media.source_summaries",
                    "local_media.get_source_summary",
                ),
            )
        )

    registry.register(
        ToolSpec(
            name="config.media_path_mappings",
            description="读取Jellyfin/Emby的STRM与本地路径前缀映射摘要；不暴露配置凭据，不等同于本地分类归档绑定。",
            risk=RiskLevel.READ,
            parameters={
                "type": "object",
                "properties": {
                    "provider": {"type": "string", "enum": ["jellyfin", "emby"]}
                },
                "required": ["provider"],
                "additionalProperties": False,
            },
            validator=mapping_arguments,
            handler=list_path_mappings,
            domains=("config", "library", "strm"),
            related_tools=(
                "config.create_media_path_mapping",
                "config.update_media_path_mapping",
            ),
            examples=("查看Jellyfin的媒体库路径映射", "STRM目录怎么映射给Emby"),
        )
    )
    for operation in ("create", "update", "delete"):
        properties = {"provider": {"type": "string", "enum": ["jellyfin", "emby"]}}
        required = ["provider"]
        if operation != "create":
            properties["mapping_number"] = {"type": "integer", "minimum": 1}
            required.append("mapping_number")
        if operation != "delete":
            properties.update(
                {
                    key: {"type": "string", "minLength": 1, "maxLength": 2048}
                    for key in ("local_path", "server_path")
                }
            )
        if operation == "create":
            required.extend(("local_path", "server_path"))
        registry.register(
            ToolSpec(
                name=f"config.{operation}_media_path_mapping",
                description={
                    "create": "添加Jellyfin/Emby路径前缀映射，使用用户给出的本地STRM目录和媒体服务器可见目录。",
                    "update": "修改一条媒体服务器路径前缀映射，保留同服务器其它映射。mapping_number来自config.media_path_mappings。",
                    "delete": "删除一条媒体库路径前缀映射，不删除媒体文件；后续该路径将不再进行前缀转换。",
                }[operation],
                risk=RiskLevel.WRITE,
                requires_confirmation=True,
                parameters={
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
                validator=partial(mapping_arguments, operation=operation),
                context_confirmation_preparer=ToolSpec.context_free_confirmation_preparer(
                    partial(prepare_path_mapping, operation=operation)
                ),
                context_confirmed_handler=ToolSpec.context_free_confirmed_handler(
                    partial(execute_path_mapping, operation=operation)
                ),
                domains=("config", "library", "strm"),
                related_tools=("config.media_path_mappings",),
            )
        )
