"""三项定时策略已签发确认的指纹兼容与唯一算法回归。"""

from __future__ import annotations

import asyncio
import json
import os
from copy import deepcopy
from unittest.mock import Mock, patch

from app import config, sdk
from app.agent.errors import ToolPipelineError
from app.agent.kernel import sandbox
from app.agent.kernel.persistence import SQLiteKernelStore
from app.modules import schedule_policy as service
from tests.support import initialized_web_runtime
from tests.test_agent_sdk_effects import context_for, executor

import pytest


@pytest.mark.parametrize(
    "kind,snapshot,expected",
    [
        (
            "organize",
            None,
            "96d5602c696c0dfa256b491cf325b023bc4f02491f3c9c574781f4e0a0ee4071",
        ),
        (
            "organize",
            b"",
            "188fb76173ee83487428e7ebb518d994918bd557d4f76c2f0fda3fb731bbda72",
        ),
        (
            "organize",
            b"non-sensitive fixture snapshot\n",
            "895bbdb5508882535f9a3292c2442480d3f17065c1f0763faeb170d3128873f8",
        ),
        (
            "patrol",
            None,
            "c38f4084abf637d2c0f88ceb31acd4aba37fd16e612df2306b267d0188025956",
        ),
        (
            "patrol",
            b"",
            "4d0e25177f7e279c0c17a779e27c706a2d00440dc6beaae81c827e1b031b15c4",
        ),
        (
            "patrol",
            b"non-sensitive fixture snapshot\n",
            "a5788ae0d645520402edfbc1d1174c6eebfa860eba845b085087e7c43f529af8",
        ),
        (
            "strm",
            None,
            "1db51050c56fd625e18786e0f10d18155b91d5a7a7ba2f1ff0f527cf26746679",
        ),
        (
            "strm",
            b"",
            "67a8d490d8a250250c60650364c987e98daf48e1ee52393dc5834dde3fc67f87",
        ),
        (
            "strm",
            b"non-sensitive fixture snapshot\n",
            "0dba62b0d9ed525c7a6d4949ca66b4f63e3c14efe06c5773281d555ca9061261",
        ),
    ],
)
def test_history_policy_fingerprint_is_byte_compatible_with_7068c54(
    kind, snapshot, expected
):
    current = {name: value[1] for name, value in service._TARGETS[kind].items()}
    with (
        patch.object(
            service.config, "read_env_snapshot", return_value=(snapshot, {})
        ) as read,
        patch.object(service, "current_policy", return_value=current),
        patch.object(service.config, "has_external_override", return_value=False),
    ):
        state = service.capture_policy_state(kind, {"enabled": not current["enabled"]})
        assert service.policy_state_fingerprint(state) == expected
        assert state["requested_keys"] == state["changed_keys"] == ("enabled",)
        read.assert_called_once_with(service.config.ENV_FILE)
        assert current["enabled"] is False  # 请求快照不得修改当前策略。


@pytest.mark.parametrize(
    "kind",
    [
        "organize",
        "patrol",
        "strm",
    ],
)
def test_normal_policy_shared_snapshot_still_invalidates_each_confirmation_barrier(
    kind,
):
    current = {name: value[1] for name, value in service._TARGETS[kind].items()}
    with (
        patch.object(service.config, "read_env_snapshot", return_value=(b"fixture", {})),
        patch.object(service, "current_policy", return_value=current),
        patch.object(service.config, "has_external_override", return_value=False),
    ):
        state = service.capture_policy_state(kind, {"enabled": True})
        fingerprint = service.policy_state_fingerprint(state)
    variants = []
    for key, value in (
        ("snapshot_present", False),
        ("snapshot_sha256", "0" * 64),
        ("requested_keys", ()),
        ("changed_keys", ()),
        ("external_overrides", ("enabled",)),
    ):
        variants.append({**state, key: value})
    for key in ("current", "requested", "persisted"):
        changed = deepcopy(state)
        name = "enabled" if key != "persisted" else service._TARGETS[kind]["enabled"][0]
        changed[key][name] = not changed[key][name] if key != "persisted" else "1"
        variants.append(changed)
    assert all(service.policy_state_fingerprint(variant) != fingerprint for variant in variants)


_POLICIES = (
    ("strm", "strm.schedule", "strm.set_schedule"),
    ("organize", "cloud.organize_schedule", "cloud.set_organize_schedule"),
    ("patrol", "library.patrol_policy", "library.set_patrol_policy"),
)


@pytest.fixture
def policy_installation():
    # 真配置发布与持久确认都落在临时安装中，并恢复各策略写入的进程环境。
    with initialized_web_runtime(), patch.dict(os.environ):
        config.set_and_save({key: str(int(value)) if type(value) is bool else str(value)
                             for targets in service._TARGETS.values() for key, value in targets.values()})
        yield


@pytest.mark.parametrize("kind,read,write", _POLICIES)
def test_native_policy_rpc_cold_plan_can_be_cancelled_without_writing(policy_installation, tmp_path, kind, read, write):
    async def run():
        before = config.read_env_snapshot(config.ENV_FILE)[0]
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        (tmp_path / "run").mkdir()
        bridge = sandbox._SDKRequests(context, tmp_path, effects)
        bridge.uid = os.getuid()
        request = tmp_path / "run/mf-requests/schedule.json"
        request.write_text(json.dumps({"method": write, "arguments": {"enabled": True}}))
        try:
            await bridge.dispatch(request.name)
            assert bridge.proposal is not None
            plan = bridge.proposal.effect_plan
            assert plan is not None and plan.arguments == {"enabled": True}
            assert "snapshot" not in json.dumps(plan.public_dict())
            assert config.read_env_snapshot(config.ENV_FILE)[0] == before
        finally:
            await bridge.close()
        # 只从SQLite读原确认，不能再次起草或执行原脚本。
        rebuilt = executor(SQLiteKernelStore())
        restored = await rebuilt.get_pending_plan(owner=context.owner, session_id=context.session_id)
        assert restored is not None and restored.plan_id == plan.plan_id
        assert await rebuilt.cancel_effect(plan.plan_id, lease=context.lease)
        with pytest.raises(ToolPipelineError):
            await rebuilt.execute_confirmed(plan.plan_id, context=context)
        assert sdk.METHODS[read]()["data"]["policy"]["enabled"] is False
        assert config.read_env_snapshot(config.ENV_FILE)[0] == before
    asyncio.run(run())


@pytest.mark.parametrize("kind,read,write", _POLICIES)
def test_direct_sdk_changes_only_given_fields_and_reports_save_before_reload_failure(policy_installation, monkeypatch, kind, read, write):
    initial = sdk.METHODS[read]()["data"]["policy"]
    reload = Mock(side_effect=RuntimeError("private-path-or-secret"))
    monkeypatch.setattr(service, "reload_schedule", reload)
    result = sdk.METHODS[write](enabled=True)
    assert result["ok"] is True and result["status"] == "completed"
    assert result["data"]["runtime_refreshed"] is False
    assert result["data"]["policy"] == {**initial, "enabled": True}
    assert sdk.METHODS[read]()["data"]["policy"] == {**initial, "enabled": True}
    assert "重启" in " ".join(result["suggestions"])
    assert "private-path-or-secret" not in repr(result)
    reload.assert_called_once_with(kind)
    # 同值重复不会再次保存/刷新，也不会丢弃其它字段。
    before = config.read_env_snapshot(config.ENV_FILE)[0]
    assert sdk.METHODS[write](enabled=True)["status"] == "no_changes"
    assert config.read_env_snapshot(config.ENV_FILE)[0] == before
    reload.assert_called_once()


@pytest.mark.parametrize("change", ["external", "stale", "race", "write_error"])
def test_schedule_policy_preconditions_and_cas_never_overwrite_other_writer(policy_installation, monkeypatch, change):
    preview, fingerprint = service.prepare_policy_change("strm", {"enabled": True})
    assert preview["status"] == "confirmation_required"
    reload = Mock()
    monkeypatch.setattr(service, "reload_schedule", reload)
    write = config.update_runtime_env_file
    if change == "external":
        monkeypatch.setattr(config, "has_external_override", lambda key: key == "STRM_SCHEDULE_ENABLED")
        with patch.object(config, "update_runtime_env_file") as update:
            assert sdk.strm.set_schedule(enabled=True)["status"] == "environment_override"
            update.assert_not_called()
    elif change == "stale":
        config.set_and_save({"STRM_SCHEDULE_CRON": "0 5 * * *"})
        with pytest.raises(ValueError, match="配置已变化"):
            service.apply_policy_change("strm", {"enabled": True}, expected_fingerprint=fingerprint)
    else:
        def racing_write(path, updates, *, expected):
            if change == "write_error":
                raise OSError("private-path-or-secret")
            write(path, {"STRM_SCHEDULE_CRON": "0 6 * * *"}, expected=expected)
            return write(path, updates, expected=expected)
        monkeypatch.setattr(config, "update_runtime_env_file", racing_write)
        result = service.apply_policy_change("strm", {"enabled": True}, expected_fingerprint=fingerprint)
        assert result["status"] == ("conflict" if change == "race" else "unavailable")
        assert result["ok"] is False and "private-path-or-secret" not in repr(result)
    assert sdk.strm.schedule()["data"]["policy"]["enabled"] is False
    reload.assert_not_called()


@pytest.mark.parametrize("failure", ["publish_unknown", "read_after_publish"])
def test_native_policy_unknown_save_receipt_consumes_plan_without_replay(policy_installation, monkeypatch, failure):
    from app import database as db
    from app.agent.kernel.effects import ConfirmationEffectPlanStore
    from app.agent.confirmation import SQLiteConfirmationStore
    from app.agent.kernel.ports.mediaflux_effects import MediaFluxEffectLifecycle

    async def run():
        store = SQLiteKernelStore()
        context = await context_for(store)
        effects = executor(store)
        effects.effect_store = ConfirmationEffectPlanStore(SQLiteConfirmationStore(), record_actions=True)
        effects.effect_lifecycle = MediaFluxEffectLifecycle()
        plan = (await effects.prepare("strm.set_schedule", {"enabled": True}, context=context)).effect_plan
        assert plan is not None
        original_write, original_read = config.update_runtime_env_file, service.current_policy
        writes = []

        def publish(path, updates, *, expected):
            writes.append(dict(updates))
            committed = original_write(path, updates, expected=expected)
            if failure == "publish_unknown":
                raise config.AtomicPublishError("private-path-after-publish")
            return committed

        def read(kind):
            policy = original_read(kind)
            if policy["enabled"]:
                raise ValueError("private-path-after-publish")
            return policy

        monkeypatch.setattr(config, "update_runtime_env_file", publish)
        if failure == "read_after_publish":
            monkeypatch.setattr(service, "current_policy", read)
        monkeypatch.setattr(service, "reload_schedule", Mock())
        outcome = (await effects.execute_confirmed(plan.plan_id, context=context)).outcome.public_content
        assert outcome["ok"] is False
        assert outcome["status"] == ("unavailable" if failure == "publish_unknown" else "outcome_unknown")
        assert outcome["data"].get("execution_started") is not False
        assert "未执行" not in repr(outcome) and "private-path-after-publish" not in repr(outcome)
        assert config.read_env_snapshot(config.ENV_FILE)[1]["STRM_SCHEDULE_ENABLED"] == "1"
        # 原票据已消费；失败回执和新预检不能授权再次提交原写入。
        cold = executor(SQLiteKernelStore())
        assert await cold.get_pending_plan(owner=context.owner, session_id=context.session_id) is None
        with pytest.raises(ToolPipelineError):
            await cold.execute_confirmed(plan.plan_id, context=context)
        assert writes == [{"STRM_SCHEDULE_ENABLED": "1"}]
        rows = db.list_agent_action_history(owner_digest=db.media_owner_digest(context.owner))
        assert len(rows) == 1 and rows[0]["status"] == outcome["status"] and not rows[0]["ok"]
    asyncio.run(run())
