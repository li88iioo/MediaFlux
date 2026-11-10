"""无需变更的真实领域预检应正常结束，不签发确认票据或执行写入。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import config
from app.repositories import discovery as discovery_repo

from app.agent.kernel.events import AgentEventType
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.ports.sdk_effects import policy_config_tool, cloud_organize_tools, feature_config_tool, rss_delivery_tools, indexer_configuration_tool, schedule_policy_tools, watchlist_tools, strm_failure_tools
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import AgentInput
from tests.support import isolated_test_database
from tests.test_agent_kernel_core import ScriptedModel, collect
from tests.test_agent_kernel_existing_ports import context_for


_WATCHLIST_ARGUMENTS = {"provider": "tmdb", "external_id": "8801", "media_type": "movie"}
_NO_CHANGES_CASES = (
    ("resources.configure_sites", {"site_ids": ["nyaa"]}),
    ("library.set_patrol_policy", {"enabled": False}),
    ("cloud.set_organize_schedule", {"enabled": False}),
    ("cloud.organize", {}),
    ("watchlist.add", _WATCHLIST_ARGUMENTS),
    ("rss.retry", {"limit": 10}),
    ("rss.download", {"limit": 10}),
    ("strm.retry_failures", {"ids": []}),
    ("settings.set_feature", {"feature": "discovery", "enabled": True}),
    ("strm.set_schedule", {"enabled": False}),
    ("settings.set_policy", {"policy": "tmdb_match_mode", "value": "strict"}),
)


class NoChangesContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        directory = self.enterContext(tempfile.TemporaryDirectory())
        env_file = Path(directory) / "user.env"
        values = {
            "DISCOVERY_ENABLED": "1",
            "INDEXER_ENABLED_SITES": "nyaa",
            "INDEXER_SITE_CONFIG_VERSION": "2",
            "INDEXER_SUKEBEI_ENABLED": "0",
            "AGENT_LIBRARY_PATROL_ENABLED": "0",
            "GY_ORGANIZE_SCHEDULE_ENABLED": "0",
            "GY_ORGANIZE_SOURCE_DIRS": '[{"id":"source","name":"Source"}]',
            "GY_ORGANIZE_TARGET_DIR": "target",
            "STRM_SCHEDULE_ENABLED": "0",
            "TMDB_MATCH_MODE": "strict",
        }
        config.write_env_file(env_file, values, replace=False)
        self.enterContext(patch.dict(os.environ, {key: "" for key in values}))
        self.enterContext(patch.object(config, "ENV_FILE", env_file))
        self.enterContext(patch.object(config, "_cache", None))
        self.enterContext(patch.object(config, "_STARTUP_ENV_OVERRIDES", frozenset()))
        self.writer = self.enterContext(patch.object(config, "update_runtime_env_file"))
        self.discovery = self.enterContext(patch(
            "app.discovery.service.DiscoveryService.get_detail"
        ))
        self.manager = Mock()
        self.manager.task_status.return_value = {"status": "idle"}
        self.enterContext(patch(
            "app.modules.organize_service.get_organize_manager",
            return_value=self.manager,
        ))
        self.enterContext(patch(
            "app.modules.organize_service.GuangYaClient",
            return_value=SimpleNamespace(logged_in=True, credential_generation=1, close=Mock()),
        ))
        self.organizer = Mock()
        self.organizer.organize.return_value = ([], {"total": 0})
        self.enterContext(patch(
            "app.modules.organize_service.Organizer",
            return_value=self.organizer,
        ))
        self.enterContext(patch(
            "app.modules.rss.capture_rss_qb_runtime_config",
            return_value=({"url": "http://qb.invalid", "default_method": "qb"}, ""),
        ))
        self.rss_submit = self.enterContext(patch("app.modules.rss.RSSEngine.submit_snapshot"))
        self.enterContext(patch(
            "app.modules.strm.capture_strm_retry_runtime_config",
            return_value=({"sources": [{"id": "source"}], "strm_root": "/strm"}, ""),
        ))
        self.strm_retry = self.enterContext(patch("app.modules.strm.retry_strm_failures"))
        discovery_repo.add_media_watchlist("tmdb", "8801", "movie", "已收藏影片", "2026", "")
        names = {name for name, _arguments in _NO_CHANGES_CASES}
        self.catalog = ToolCatalog()
        for tool in (policy_config_tool(), *cloud_organize_tools(), feature_config_tool(), indexer_configuration_tool(), *watchlist_tools(), *rss_delivery_tools(), *schedule_policy_tools(), *strm_failure_tools()):
            if tool.name in names:
                self.catalog.register(tool)
        self.state = InMemorySessionStateStore()
        self.pipeline = ToolPipeline(catalog=self.catalog, state_store=self.state)
        self.freeze = self.enterContext(patch.object(
            self.pipeline.effect_store, "freeze", wraps=self.pipeline.effect_store.freeze
        ))

    def tearDown(self):
        self.freeze.assert_not_called()
        self.writer.assert_not_called()
        self.discovery.assert_not_called()
        self.manager.start.assert_not_called()
        self.rss_submit.assert_not_called()
        self.strm_retry.assert_not_called()

    async def test_all_eleven_domain_preparations_finish_without_a_confirmation(self):
        self.assertEqual(len(_NO_CHANGES_CASES), 11)
        for name, arguments in _NO_CHANGES_CASES:
            with self.subTest(tool=name):
                context = await context_for(self.state, session=name)
                result = await self.pipeline.execute(name, arguments, context=context)
                public = result.outcome.public_content
                self.assertIs(public["ok"], True)
                self.assertEqual(public["status"], "no_changes")
                self.assertEqual(public["error"], "")
                self.assertTrue(public["summary"])
                self.assertIsNone(result.effect_plan)
                state = await self.state.load(owner=context.owner, session_id=name)
                self.assertFalse(state.pending_effect_plan_id)
        self.organizer.organize.assert_called_once()
        self.assertIs(self.organizer.organize.call_args.kwargs["dry_run"], True)

    async def test_duplicate_watchlist_finishes_a_real_session_successfully(self):
        tool = self.catalog.get("watchlist.add")
        model = ScriptedModel([
            [ModelEvent(ModelEventType.TOOL_CALL_COMPLETED,
                        tool_call=ModelToolCall("already-saved", tool.model_name, _WATCHLIST_ARGUMENTS)),
             ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")],
            [ModelEvent(ModelEventType.TEXT_DELTA, text="该影视条目已在探索收藏中，无需重复添加。"),
             ModelEvent(ModelEventType.FINISH, finish_reason="stop")],
        ])
        session = AgentSession(
            model=model, catalog=self.catalog,
            pipeline=self.pipeline, state_store=self.state,
        )
        events = await collect(session.run(AgentInput(
            message="把 tmdb 编号 8801 的电影加入探索收藏", owner="owner", session_id="watchlist",
        )))
        self.assertEqual(events[-1].type, AgentEventType.TURN_COMPLETED,
                         [event.to_dict() for event in events])
        self.assertEqual(events[-1].payload["status"], "success")
        self.assertEqual(len(model.requests), 2)
        results = [event.payload["result"] for event in events
                   if event.type == AgentEventType.TOOL_COMPLETED]
        self.assertEqual(len(results), 1)
        self.assertIs(results[0]["ok"], True)
        self.assertEqual(results[0]["status"], "no_changes")
        self.assertEqual(results[0]["error"], "")
        self.assertFalse(any(event.type in {
            AgentEventType.TOOL_FAILED, AgentEventType.EFFECT_APPROVAL_REQUIRED,
            AgentEventType.EFFECT_COMPLETED, AgentEventType.EFFECT_FAILED,
        } for event in events))
        facts = json.loads(next(message.content for message in model.requests[1].messages
                                if message.role == "tool"))
        self.assertEqual((facts["ok"], facts["status"]), (True, "no_changes"))
        state = await self.state.load(owner="owner", session_id="watchlist")
        self.assertFalse(state.pending_effect_plan_id)
        self.assertEqual(len(discovery_repo.list_media_watchlist()), 1)
