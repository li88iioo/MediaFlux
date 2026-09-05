"""真实目录的中文能力可达性；不访问配置、Provider、调度器或真实媒体。

这些用例只验证生产默认召回预算及引用上游可达性，不将词法召回成功
等同于真实模型已经正确理解或业务已经执行。
"""

from __future__ import annotations

import json

import pytest

from app.agent.domain_catalog import build_tool_specs
from app.agent.kernel.capabilities import CapabilityRetriever
from app.agent.kernel.ports.existing_actions import catalog_from_tool_specs
from app.agent.provider_artifacts import ProviderArtifactStore
from app.agent.provider_models import ProviderGatewayError
from app.agent.provider_operations import media_management_specs


@pytest.fixture(scope="module")
def catalog():
    return catalog_from_tool_specs(build_tool_specs())


def select(catalog, message, *, recent_tools=(), recent_messages=(), ref_kinds=()):
    selected = CapabilityRetriever().retrieve(
        message,
        catalog,
        context={
            "recent_tool_names": recent_tools,
            "recent_user_messages": recent_messages,
            "reference_kinds": ref_kinds,
        },
    )
    assert 6 <= len(selected.names) <= 12
    assert len(selected.names) == len(set(selected.names))
    return set(selected.names)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "刚才下载的绿灯军团现在到哪一步了",
            {"activity.search", "activity.timeline"},
        ),
        (
            "帮我盯着刚才的下载，入库完成后通知我",
            {"activity.follow", "activity.search", "activity.follows"},
        ),
        ("不用再跟踪这个任务了", {"activity.unfollow", "activity.follows"}),
        ("撤销刚才修改的配置", {"action.undo.execute"}),
        (
            "以后优先下载4K杜比视界带中文字幕的版本",
            {"media.set_preferences", "media.preferences"},
        ),
        ("我设置了哪些选片和下载偏好", {"media.preferences"}),
        (
            "看看动漫库里有哪些720p或者缺中文字幕的作品",
            {"media.library.quality", "provider.capabilities", "provider.query"},
        ),
        (
            "把刚才推荐的五部电影放到新建的周末看片列表",
            {"media.playlist.create", "provider.capabilities"},
        ),
        (
            "把刚才的电影加入周末看片播放列表",
            {"media.playlist.add_items", "provider.capabilities"},
        ),
        (
            "从周末看片列表移除第一部，保留文件",
            {"media.playlist.remove_items", "media.playlist.inspect"},
        ),
        (
            "把刚才的电影收藏一下",
            {"media.user.favorite", "provider.capabilities"},
        ),
        ("这部电影我看完了，标记已看", {"media.user.mark_played"}),
        (
            "每天晚上9点告诉我今天入库了什么，只在失败时提醒下载任务",
            {"automation.set_digest", "automation.digest_rules", "media.today_summary"},
        ),
        ("我配置了哪些每日摘要", {"automation.digest_rules"}),
        (
            "添加本地媒体来源，目录是/media/downloads，名字叫动漫",
            {"config.create_local_source", "local_media.source_summaries"},
        ),
        (
            "新增Jellyfin路径映射，从/media/strm到/data/media",
            {"config.create_media_path_mapping", "config.media_path_mappings"},
        ),
        (
            "修改Jellyfin第一条媒体库路径映射",
            {"config.update_media_path_mapping", "config.media_path_mappings"},
        ),
        (
            "把Dynamis One添加到发布组识别知识库",
            {"config.create_recognition_knowledge", "config.recognition_knowledge"},
        ),
        (
            "每周检查光阴之外缺集，有资源自动推送到光鸭",
            {"automation.create_media_rule", "media.subscription_summaries"},
        ),
    ],
)
def test_common_chinese_requests_retrieve_new_capabilities(catalog, message, expected):
    names = select(catalog, message)
    assert expected <= names, f"missing={expected - names}; selected={sorted(names)}"


@pytest.mark.parametrize(
    ("message", "recent_tools", "recent_messages", "ref_kinds", "expected"),
    [
        (
            "帮我盯着它",
            ("activity.timeline",),
            ("刚才下载的绿灯军团现在到哪了",),
            ("activity_selection",),
            {"activity.follow"},
        ),
        (
            "不用再跟踪了",
            ("activity.follows",),
            ("正在帮我跟踪哪些任务",),
            (),
            {"activity.unfollow", "activity.follows"},
        ),
        (
            "关闭每天的摘要",
            ("automation.digest_rules",),
            ("我设置了哪些每日摘要",),
            (),
            {"automation.set_digest", "automation.digest_rules"},
        ),
        (
            "撤销刚才的操作",
            ("media.set_preferences",),
            ("以后优先4K",),
            ("undo_receipt",),
            {"action.undo.execute"},
        ),
        (
            "撤销刚才的操作",
            ("config.set_feature_state",),
            ("停用自动整理",),
            ("undo_receipt",),
            {"action.undo.execute"},
        ),
        (
            "把这几部加进去",
            ("media.playlist.inspect",),
            ("查看周末看片播放列表",),
            (),
            {"media.playlist.add_items", "media.playlist.inspect"},
        ),
    ],
)
def test_short_followups_keep_current_capabilities(
    catalog, message, recent_tools, recent_messages, ref_kinds, expected
):
    names = select(
        catalog,
        message,
        recent_tools=recent_tools,
        recent_messages=recent_messages,
        ref_kinds=ref_kinds,
    )
    assert expected <= names, f"missing={expected - names}; selected={sorted(names)}"


def test_move_back_retrieves_snapshot_based_undo_not_only_another_move(catalog):
    names = select(
        catalog,
        "把刚才移动的目录移回去",
        recent_tools=("guangya.directory_scrape.run",),
        recent_messages=("把光鸭动漫目录整理入库",),
    )
    # 重新发起普通 move 不能代替核对已完成任务的反向快照。
    assert {"action.undo.inspect", "activity.search"} <= names, sorted(names)


def test_recommendation_to_existing_playlist_can_acquire_playlist_reference(catalog):
    names = select(
        catalog,
        "这两部放进周末看片",
        recent_tools=("media.recommend_from_library",),
        recent_messages=("从我库里推荐两部电影",),
    )
    assert "media.playlist.add_items" in names
    assert "provider.capabilities" in names  # 获得真实 profile_ref，而非猜服务器名。
    # 推荐只返回媒体项引用；尚无目标列表引用。inspect 不能凭空接收列表 ID。
    # 通用 READ Provider 入口可替代未召回的原子 list/inspect，不能误报为双轨。
    assert "media.playlists.list" in names or "provider.query" in names, sorted(names)
    assert "media.playlist.inspect" in names or "provider.query" in names, sorted(names)


def test_favorite_has_a_read_path_for_bound_user_item_snapshot(catalog):
    names = select(catalog, "把刚才的电影收藏一下")
    assert "media.user.favorite" in names
    assert "provider.capabilities" in names
    assert "media.user.inspect" in names or "provider.query" in names


def test_new_atomic_catalog_related_tools_resolve_to_registered_specs(catalog):
    prefixes = (
        "activity.",
        "action.undo.",
        "automation.",
        "media.playlist",
        "media.user.",
    )
    new_config_names = {
        "config.recognition_knowledge",
        "config.create_recognition_knowledge",
        "config.update_recognition_knowledge",
        "config.delete_recognition_knowledge",
        "config.create_local_source",
        "config.update_local_source",
        "config.delete_local_source",
        "config.media_path_mappings",
        "config.create_media_path_mapping",
        "config.update_media_path_mapping",
        "config.delete_media_path_mapping",
    }
    inspected = 0
    for spec in catalog.visible():
        if spec.name.startswith(prefixes) or spec.name in new_config_names:
            inspected += 1
            assert spec.description.strip()
            for related in spec.metadata.get("related_tools", ()):
                assert catalog.has(related), f"{spec.name} -> unknown {related}"
    assert inspected >= 25


@pytest.mark.parametrize(
    ("operation", "argument", "kind", "source"),
    [
        ("media.user.mark_played", "item_ref", "media_user_item", "media.user.inspect"),
        (
            "media.user.mark_unplayed",
            "item_ref",
            "media_user_item",
            "media.user.inspect",
        ),
        ("media.user.favorite", "item_ref", "media_user_item", "media.user.inspect"),
        ("media.user.unfavorite", "item_ref", "media_user_item", "media.user.inspect"),
        (
            "media.playlist.add_items",
            "playlist_ref",
            "media_playlist_snapshot",
            "media.playlist.inspect",
        ),
        (
            "media.playlist.remove_items",
            "entry_refs",
            "media_playlist_entry",
            "media.playlist.inspect",
        ),
    ],
)
def test_write_reference_contract_names_actual_snapshot_source(
    catalog, operation, argument, kind, source
):
    specs = {spec.operation_id: spec for spec in media_management_specs()}
    spec = specs[operation]
    assert spec.reference_arguments[argument] == kind
    assert source in spec.description
    atom = catalog.get(operation)
    assert "profile_ref" in atom.input_schema["required"]
    assert catalog.has(source)


@pytest.mark.parametrize(
    "kind",
    [
        "media_item",
        "media_user_item",
        "media_library",
        "media_playlist",
        "media_playlist_snapshot",
        "media_playlist_entry",
    ],
)
def test_provider_reference_roundtrip_keeps_type_owner_and_session(kind):
    store = ProviderArtifactStore()
    identity = {
        "owner": "webk:v1:test-owner",
        "session_id": "capability-audit-session",
        "provider": "media",
        "profile_ref": "configured:jellyfin",
    }
    _artifact, data = store.put(
        **identity,
        operation="audit.fixture",
        data={
            "item": {
                "__object_id": "private-provider-id",
                "__object_kind": kind,
                "title": "测试媒体",
            }
        },
    )
    reference = data["item"]["object_ref"]
    assert reference.startswith("PO-")
    assert "private-provider-id" not in json.dumps(data)
    raw_id, _snapshot = store.resolve_object(
        **identity, object_ref=reference, expected_kind=kind
    )
    assert raw_id == "private-provider-id"
    for altered in (
        {"owner": "webk:v1:other-owner"},
        {"session_id": "other-session"},
        {"profile_ref": "configured:emby"},
    ):
        with pytest.raises(ProviderGatewayError):
            store.resolve_object(
                **{**identity, **altered}, object_ref=reference, expected_kind=kind
            )
    with pytest.raises(ProviderGatewayError):
        store.resolve_object(
            **identity, object_ref=reference, expected_kind="wrong_reference_kind"
        )


def test_new_capabilities_have_readable_public_labels_and_write_audit_names():
    from app.agent.action_history import _safe_tool_name
    from app.agent.confirmation_contract import build_confirmation_contract
    from app.agent.models import RiskLevel, ToolResult
    from app.agent.public_safety import public_tool_label

    new_names = {
        "activity.search",
        "activity.timeline",
        "activity.follow",
        "activity.follows",
        "activity.unfollow",
        "action.undo.inspect",
        "action.undo.execute",
        "media.library.quality",
        "media.user.inspect",
        "media.user.mark_played",
        "media.user.mark_unplayed",
        "media.user.favorite",
        "media.user.unfavorite",
        "media.playlists.list",
        "media.playlist.inspect",
        "media.playlist.create",
        "media.playlist.add_items",
        "media.playlist.remove_items",
        "automation.create_media_rule",
        "automation.digest_rules",
        "automation.set_digest",
        "config.recognition_knowledge",
        "config.create_recognition_knowledge",
        "config.update_recognition_knowledge",
        "config.delete_recognition_knowledge",
        "config.create_local_source",
        "config.update_local_source",
        "config.delete_local_source",
        "config.media_path_mappings",
        "config.create_media_path_mapping",
        "config.update_media_path_mapping",
        "config.delete_media_path_mapping",
    }
    specs = {spec.name: spec for spec in build_tool_specs()}
    for name in new_names:
        spec = specs[name]
        label = public_tool_label(name)
        assert label != "MediaFlux 检查", name
        if spec.risk != RiskLevel.READ:
            assert _safe_tool_name(name) == name, name
            contract = build_confirmation_contract(
                tool_name=name,
                risk=spec.risk,
                preview=ToolResult(True, "confirmation_required", "尚未执行"),
            )
            assert contract["action"] == label
            assert contract["impact"] != "确认后会执行服务端预检通过的受控操作。", name
