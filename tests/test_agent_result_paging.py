"""工具证据保真：实际Pipeline/加密SQLite引用/预算压缩，不访问外部Provider。"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.model import ModelMessage
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.pipeline import ToolCallContext, ToolPipeline, ToolPipelineError
from app.agent.kernel.projection import DefaultProjector
from app.agent.kernel.state import CancellationToken
from app.agent.model_context_budget import _compact_chain, compact_tool_content, decode_tool_content
from tests.support import IsolatedDatabaseTestCase


class ResultPagingTests(IsolatedDatabaseTestCase):
    def test_small_fact_survives_large_result_compression(self):
        fact = ModelMessage(role="tool", content='{"ok":true,"data":{"important_fact":"KEEP_ME"}}')
        large = replace(fact, content=json.dumps({"data": {"items": ["large" * 2000] * 40}}))
        compacted = _compact_chain([fact, large], max_tool_chars=400)
        self.assertIs(compacted[0], fact)
        self.assertLessEqual(len(compacted[1].content), 400)
        self.assertTrue(json.loads(compacted[1].content)["truncated"])

    def test_reference_appendix_is_counted_and_legacy_history_is_readable(self):
        old = '{"ok":true,"data":{"count":200}}\nopaque_refs=[]\nreference_arguments={}\ncandidate_numbers=' + json.dumps([{"title": "x" * 100}] * 200)
        content = compact_tool_content(old, maximum=400)
        self.assertLessEqual(len(content), 400)
        self.assertTrue(json.loads(content)["truncated"])
        legacy = 'opaque_refs=[{"kind":"resource_candidates","ref":"old-ref"}]'
        self.assertEqual(decode_tool_content(legacy)["opaque_refs"][0]["ref"], "old-ref")

    def test_projection_does_not_silently_cut_small_arrays_strings_or_maps(self):
        data = {"items": list(range(501)), "text": "z" * 2100, "fields": {str(n): n for n in range(201)}}
        outcome = DefaultProjector().project({"ok": True, "data": data})
        self.assertEqual(json.loads(outcome.model_content)["data"], data)
        self.assertEqual(outcome.public_content["data"], data)
        self.assertFalse(outcome.full_model_content)

    def test_large_results_can_be_read_completely_after_runtime_recreation(self):
        async def run():
            data = [{"title": f"作品-{n}-" + "介绍" * 650, "tmdb_id": str(n)} for n in range(100)]
            store = SQLiteKernelStore(secret_provider=lambda: "result-paging-test")
            lease, _ = await store.begin_turn(owner="owner", session_id="session", request_id="query")
            context = ToolCallContext(owner="owner", session_id="session", request_id=lease.request_id,
                                      turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(), report_progress=AsyncMock())
            catalog = ToolCatalog([KernelToolSpec(
                name="library.inventory", domain="library", effect=ToolEffect.READ, description="媒体库库存",
                input_schema={"type": "object", "properties": {}},
                read=lambda _a, _c: {"ok": True, "status": "success", "data": {"items": data, "total": 100, "cookie": "secret-not-for-model"}},
            )])
            pipeline = ToolPipeline(catalog=catalog, state_store=store, reference_store=store, projector=DefaultProjector(max_model_chars=2000))
            result = await pipeline.execute("library.inventory", {}, context=context)
            content = result.outcome.model_content
            view = json.loads(content)
            self.assertLessEqual(len(content), 2000)
            self.assertTrue(view["truncated"])
            self.assertIn("/data/items", [row["path"] for row in view["truncation"]])
            self.assertEqual(view["read_tool"], "agent.read_result")
            handle = view["result_handle"]
            # 重建存储和Pipeline后继续读取同一加密快照，不重复外部查询。
            restored = SQLiteKernelStore(secret_provider=lambda: "result-paging-test")
            reader = ToolPipeline(catalog=ToolCatalog([]), state_store=restored, reference_store=restored,
                                  projector=DefaultProjector(max_model_chars=24000))
            received, offset = [], 0
            while True:
                page = await reader.execute("agent.read_result", {"handle": handle, "path": "/data/items", "offset": offset, "limit": 100}, context=context)
                self.assertLessEqual(len(page.outcome.model_content), 24000)
                payload = json.loads(page.outcome.model_content)
                self.assertNotIn("secret-not-for-model", page.outcome.model_content)
                received.extend(payload["data"]["items"])
                if not payload["data"]["has_more"]:
                    break
                self.assertGreater(payload["data"]["next_offset"], offset)
                offset = payload["data"]["next_offset"]
            self.assertEqual(received, data)
            with self.assertRaises(ToolPipelineError) as missing:
                await reader.execute("agent.read_result", {"handle": handle, "path": "/data/missing"}, context=context)
            self.assertEqual(missing.exception.code, "invalid_arguments")
            for other in (replace(context, owner="other"), replace(context, session_id="other")):
                with self.assertRaises(ToolPipelineError) as caught:
                    await reader.execute("agent.read_result", {"handle": handle, "path": "/data/items"}, context=other)
                self.assertEqual(caught.exception.code, "reference_invalid")
            restored._clock = lambda: 10**12
            with self.assertRaises(ToolPipelineError) as expired:
                await reader.execute("agent.read_result", {"handle": handle, "path": "/data/items"}, context=context)
            self.assertEqual(expired.exception.code, "reference_invalid")
        asyncio.run(run())
