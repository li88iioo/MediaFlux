"""冷 root 工作区经真实 file IPC 调用媒体 SDK；所有外部提供者为隔离假数据。"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.state import InMemorySessionStateStore
from app.discovery.models import DiscoveryPage, MediaCard, ProviderHealth, ProviderTimeout
from app.discovery.registry import ProviderRegistry
from app.discovery.search import DiscoverySearchResult, DiscoverySearchService
from app.discovery.service import DiscoveryService
from tests.support import isolated_test_database
from tests.test_agent_anime_calendar import calendar_service
from tests.test_agent_code_sandbox import make_context


class MetadataProvider:
    def __init__(self, name):
        self.name, self.calls = name, []

    def card(self, index, media_type="tv"):
        return MediaCard(self.name, str(index), media_type, f"作品-{index}", year="2026",
                         tmdb_id=str(index) if self.name == "tmdb" else "",
                         origin_country=("CN", "US"), original_language="zh", genre_ids=(16,),
                         weekday=4 if self.name == "bangumi" else None)

    def search(self, query, page):
        self.calls.append(("search", query, page))
        return [self.card(i) for i in range(1, 31)], True

    def list_items(self, category, media_type, page, filters):
        self.calls.append(("list", category, page, filters))
        return DiscoveryPage(tuple(self.card(i, media_type) for i in range(1, 31)),
                             page=page, has_more=page < 2, provider=ProviderHealth(name=self.name))

    def get_detail(self, external_id, media_type):
        self.calls.append(("detail", external_id, media_type))
        return self.card(int(external_id), media_type)

    def close(self):
        pass


@unittest.skipUnless(sandbox.sandbox_supported(), "需要可运行 Bubblewrap 的 Linux 隔离工作区")
class NativeMediaWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.addCleanup(os.umask, os.umask(0o077))
        temp = self.enterContext(tempfile.TemporaryDirectory(prefix="native-media-"))
        self.directory = Path(temp)
        self.enterContext(isolated_test_database())
        self.enterContext(patch.dict(os.environ, {"DISCOVERY_ENABLED": "1"}))
        self.enterContext(patch.object(sandbox, "get_runtime_paths", return_value=SimpleNamespace(
            data_dir=self.directory, runtime_dir=self.directory / ("long-runtime-" * 10))))
        self.providers = {name: MetadataProvider(name) for name in ("tmdb", "bangumi")}
        self.discovery = DiscoveryService(registry=ProviderRegistry(self.providers))
        self.search = DiscoverySearchService(providers={"tmdb": self.providers["tmdb"]})
        self.calendar, self.calendar_sources = calendar_service()
        for service in (self.discovery, self.search, self.calendar):
            self.addCleanup(service.shutdown)
        for target, service in (("app.discovery.search.get_discovery_search_service", self.search),
                                ("app.discovery.service.get_discovery_service", self.discovery),
                                ("app.discovery.calendar.service.get_calendar_service", self.calendar)):
            self.enterContext(patch(target, return_value=service))
        self.store = InMemorySessionStateStore()
        self.pipeline = ToolPipeline(catalog=ToolCatalog(), state_store=self.store)
        self.pipeline.catalog.register(sandbox.run_code_tool(self.pipeline))
        self.context = await make_context(self.store)

    async def execute(self, code):
        result = await self.pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": code}, context=self.context)
        return json.loads(result.outcome.full_model_content or result.outcome.model_content)["data"]

    async def test_cold_native_python_cli_full_responses_and_saved_work_readback(self):
        code = """import json, subprocess
from mf.media import search, detail, catalog, calendar
facts = {
    "search": search("作品", providers=["tmdb"], media_type="tv", year="2026"),
    "detail": detail("tmdb", "tv", "42"),
    "page": catalog("tmdb", "discover", "tv"),
    "collected": catalog("tmdb", "discover", "tv", limit=3, genre="16", region="CN", year_from="2016"),
    "bangumi": catalog("bangumi", "calendar", "tv", filters={"weekday":"4"}),
    "calendar": calendar(),
}
facts["cli"] = json.loads(subprocess.check_output(["mf", "media.detail", "--provider", "tmdb", "--media-type", "tv", "--external-id", "42"]))
for card in [facts["search"]["items"][0], facts["detail"]["item"], facts["page"]["items"][0], facts["collected"]["items"][0], facts["cli"]["item"]]:
    assert card["origin_country"] == ["CN", "US"] and card["original_language"] == "zh" and card["genre_ids"] == [16]
json.dump(facts, open("media-facts.json", "w"))
print(len(facts["search"]["items"]), len(facts["page"]["items"]), facts["collected"]["scope"]["next_offset"], len(facts["calendar"]["days"]))
"""
        result = await self.execute(code)
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(result["stdout"].strip(), "30 30 3 7")
        calls = result["sdk_calls"]
        self.assertEqual([call["method"] for call in calls],
                         ["media.search", "media.detail", "media.catalog", "media.catalog", "media.catalog", "media.calendar", "media.detail"])
        workspace, _ = sandbox._workspace_location(self.context.owner, self.context.session_id)
        observations = [json.loads((workspace / call["response_path"].lstrip("/")).read_text()) for call in calls]
        self.assertEqual(observations[0]["arguments"]["year"], "2026")
        self.assertIsNone(observations[1]["result"]["item"]["number_of_seasons"])
        self.assertTrue(observations[1]["result"]["item"]["mapping_confirmed"])
        self.assertEqual(len(observations[5]["result"]["days"]), 7)
        self.assertTrue(any(call[0] == "list" and call[3].get("with_genres") == "16"
                            and call[3].get("with_origin_country") == "CN"
                            and call[3].get("first_air_date.gte") == "2016-01-01"
                            for call in self.providers["tmdb"].calls))
        replay = await self.execute('import json\nv=json.load(open("media-facts.json"))\nprint(v["cli"]["item"]["external_id"], sum(len(d["items"]) for d in v["calendar"]["days"]))')
        self.assertEqual(replay["stdout"].strip(), "42 54")
        self.assertEqual(replay["sdk_calls"], [])

    async def test_calendar_publishes_usable_week_with_optional_metadata_pending_over_real_ipc(self):
        self.calendar.metadata = SimpleNamespace(enrich=lambda entries: [
            {**entry.to_dict(), "mapping_status": "pending"} for entry in entries
        ])
        result = await self.execute('from mf.media import calendar\nv=calendar()\n'
                                    'print(v["refreshing"], v["metadata_pending"], len(v["days"]), '
                                    'sum(len(d["items"]) for d in v["days"]))')
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(result["stdout"].strip(), "False True 7 54")
        self.assertEqual([call["method"] for call in result["sdk_calls"]], ["media.calendar"])
        self.assertTrue(all(source.calls == 1 for source in self.calendar_sources.values()))

    async def test_rating_python_cli_and_argument_retry_keep_movie_identity(self):
        page = DiscoverySearchResult(query="同名作品", page=1, items=(
            MediaCard("douban", "201", "tv", "同名作品", year="2026", rating=6.4),
            MediaCard("douban", "202", "movie", "同名作品", year="2026", rating=8.1),
        ), has_more=False, errors=(), providers_attempted=("douban",), providers_succeeded=("douban",))
        with patch.object(self.search, "search", return_value=page) as search, patch(
            "app.discovery.ratings.get_discovery_search_service", return_value=self.search,
        ):
            result = await self.execute('''import json, subprocess
from mf import media
try:
    media.rating("同名作品", media_type="movie", year="bad-year")
except RuntimeError:
    print("参数未执行")
value = media.rating("同名作品", media_type="movie", year=2026, allow_web_fallback=False)
cli = json.loads(subprocess.check_output(["mf", "media.rating", "--query", "同名作品", "--media-type", "movie", "--year", "2026", "--no-allow-web-fallback"]))
assert value["data"] == cli["data"]
json.dump(value, open("rating.json", "w"))
print(value["data"]["media_type"], value["data"]["year"], value["data"]["rating"])
''')
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(result["stdout"].strip().splitlines(), ["参数未执行", "movie 2026 8.1"])
        self.assertEqual(search.call_count, 2)
        self.assertEqual([call["ok"] for call in result["sdk_calls"]], [False, True, True])
        replay = await self.execute('import json\nv=json.load(open("rating.json"))\nprint(v["data"]["source_method"], v["data"]["rating"])')
        self.assertEqual(replay["stdout"].strip(), "douban_search 8.1")
        self.assertEqual(replay["sdk_calls"], [])

    async def test_real_broker_host_cancel_stops_collector_before_next_page(self):
        from app.services import collect_discovery_recommendations

        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def collect(*args, **kwargs):
            try:
                return collect_discovery_recommendations(*args, **kwargs)
            finally:
                finished.set()
        self.enterContext(patch("app.services.collect_discovery_recommendations", side_effect=collect))
        original = self.providers["tmdb"].list_items
        def page(*args):
            entered.set()
            assert release.wait(3)
            result = original(*args)
            return DiscoveryPage(result.items[:1], has_more=True, provider=result.provider)
        self.enterContext(patch.object(self.providers["tmdb"], "list_items", side_effect=page))
        task = asyncio.create_task(self.execute('from mf.media import catalog\ncatalog("tmdb", "discover", "tv", limit=3)'))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            self.context.cancellation.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            self.assertTrue(await asyncio.to_thread(finished.wait, 3))
            self.assertEqual(len([call for call in self.providers["tmdb"].calls if call[0] == "list"]), 1)
        finally:
            release.set()
            await asyncio.to_thread(finished.wait, 3)
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_real_sdk_reports_unknown_inventory_source_failure_and_parameter_rejection(self):
        with patch("app.services._configured_media_sources", return_value=[]), patch.object(
            self.providers["tmdb"], "search", side_effect=ProviderTimeout("token=private-provider")
        ):
            result = await self.execute("""from mf import media
unknown = media.catalog("tmdb", "discover", "tv", limit=1, exclude_library=True)
failed = media.search("作品", providers=["tmdb"])
print(unknown["status"], unknown["returned"], unknown["library_check"]["counts"]["indeterminate"])
print(failed["status"], len(failed["errors"]))
try:
    media.search("作品", year="not-a-year")
except RuntimeError:
    print("参数未执行")
""")
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual(result["stdout"].strip().splitlines(), ["partial 0 30", "unavailable 1", "参数未执行"])
        self.assertEqual([call["ok"] for call in result["sdk_calls"]], [True, False, False])
        self.assertNotIn("private-provider", json.dumps(result))
