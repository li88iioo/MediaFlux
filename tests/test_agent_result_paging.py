"""工具证据保真：实际Pipeline/加密SQLite引用/预算压缩，不访问外部Provider。"""
from __future__ import annotations
from typing import Any

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from app.agent.errors import ToolPipelineError
from app.agent.kernel.capabilities import KernelToolSpec, ToolCatalog, ToolEffect
from app.agent.kernel.conversation_state import retain_conversation
from app.agent.kernel.model import ModelMessage
from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.kernel.effects import InMemoryRateLimiter
from app.agent.kernel.session_service import SessionService
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.projection import DefaultProjector
from app.agent.kernel.state_types import CancellationToken
from app.agent.kernel.tool_calls import ToolCallContext
from app.agent.model_context_budget import (
    _compact_chain,
    compact_tool_content,
    decode_tool_content,
)
from app.agent.models import ToolResult
from tests.support import IsolatedDatabaseTestCase


class ResultPagingTests(IsolatedDatabaseTestCase):
    async def snapshot_reader(self, data, *, raw=False):
        observed, read, _ = await self.snapshot_fixture(data, raw=raw)
        return observed, read

    async def snapshot_fixture(self, data, *, raw=False):
        calls = []
        store = SQLiteKernelStore(secret_provider=lambda: "coordinate-test")
        lease, _ = await store.begin_turn(owner="owner", session_id="coordinates", request_id="query")
        context = ToolCallContext(owner="owner", session_id="coordinates", request_id=lease.request_id,
                                  turn_id=lease.turn_id, lease=lease, cancellation=CancellationToken(), report_progress=AsyncMock())

        def observe(*_):
            calls.append(True)
            return data if raw else {"data": data}

        catalog = ToolCatalog([KernelToolSpec(name="library.inventory", domain="library", effect=ToolEffect.READ,
                              description="库存", input_schema={"type": "object", "properties": {}}, read=observe)])
        pipeline = ToolPipeline(catalog=catalog, state_store=store, reference_store=store,
                                projector=DefaultProjector(max_model_chars=2000))
        observed = json.loads((await pipeline.execute("library.inventory", {}, context=context)).outcome.model_content)
        message = ModelMessage(role="tool", tool_name="library.inventory", content=json.dumps(observed, ensure_ascii=False))
        await store.commit(lease, conversation=[message.to_dict()])
        restored = SQLiteKernelStore(secret_provider=lambda: "coordinate-test")
        history = (await restored.load(owner="owner", session_id="coordinates")).conversation
        observed = json.loads(ModelMessage.from_dict(history[0]).content)
        reader = ToolPipeline(catalog=ToolCatalog([]), state_store=restored, reference_store=restored,
                              projector=DefaultProjector(max_model_chars=2000), rate_limiter=InMemoryRateLimiter(limit=10000))

        async def read(path, offset=0, limit=1000, *, history_budgets=(), storage_bytes=160 * 1024):
            # 旧持久分页是历史解码夹具；活动读取只返回完整节点，Python 自行切片。
            full = await SessionService(reader, context).result(observed["result_handle"], path)
            node = full["data"]
            kind = type(node).__name__
            values = [{"key": k, "value": v} for k, v in node.items()] if isinstance(node, dict) else node if isinstance(node, (list, str)) else [node]
            items = values[offset:offset + limit]
            end = offset + len(items)
            page = {"ok": True, "status": "success", "result_handle": full["result_handle"], "read_tool": "agent.read_result",
                    "data": {"path": path, "kind": kind, "offset": offset, "total": len(values), "items": items,
                             "returned": len(items), "has_more": end < len(values), "next_offset": end if end < len(values) else None}}
            content = compact_tool_content(json.dumps(page, ensure_ascii=False), maximum=2000, result_page=True, trusted=True)
            self.assertNotIn("secret-not-for-model", content)
            self.assertNotIn("/srv/private/file", content)
            self.assertEqual(calls, [True])
            message = ModelMessage(role="tool", tool_name="agent.read_result", content=content)
            for maximum in history_budgets:
                message = _compact_chain([message], max_tool_chars=maximum)[0]
                conversation = retain_conversation([message.to_dict()], maximum_bytes=storage_bytes)
                self.assertEqual(len(conversation), 1)
                await restored.commit(lease, conversation=conversation)
                reopened = SQLiteKernelStore(secret_provider=lambda: "coordinate-test")
                saved = (await reopened.load(owner="owner", session_id="coordinates")).conversation
                message = ModelMessage.from_dict(saved[0])
                self.assertEqual(message.tool_name, "agent.read_result")
            page = json.loads(message.content)
            self.assertEqual(page["result_handle"], observed["result_handle"])
            return page

        async def retain(message, maximum_bytes=900):
            rows = retain_conversation([message.to_dict()], maximum_bytes=maximum_bytes)
            self.assertEqual(len(rows), 1)
            await restored.commit(lease, conversation=rows)
            reopened = SQLiteKernelStore(secret_provider=lambda: "coordinate-test")
            saved = (await reopened.load(owner="owner", session_id="coordinates")).conversation
            self.assertEqual(len(saved), 1)
            return ModelMessage.from_dict(saved[0])

        return observed, read, retain

    def test_business_page_fields_are_not_reader_coordinates(self):
        async def run(complete_page_shape):
            data = {"path": "/catalog", "kind": "list", "offset": 11, "items": [{"description": "D" * 10000}],
                    "total": 20, "business_count": 73}
            if complete_page_shape:
                data.update(returned=1, has_more=True, next_offset=12)
            names = ("path", "kind", "offset", "total", "business_count") + (("returned", "has_more", "next_offset") if complete_page_shape else ())
            observed, read = await self.snapshot_reader(ToolResult(ok=True, status="success", summary="business data", data=data), raw=True)
            self.assertEqual(observed["truncation"][0]["path"], "/data/items/0/description")
            for name in names:
                self.assertEqual(observed["data"][name], data[name])
            if not complete_page_shape:
                self.assertFalse({"returned", "has_more", "next_offset"} & observed["data"].keys())
            message = ModelMessage(role="tool", tool_name="library.inventory", content=json.dumps(observed))
            for maximum in (1200, 800):
                message = _compact_chain([message], max_tool_chars=maximum)[0]
                view = json.loads(message.content)
                for name in names:
                    self.assertEqual(view["data"][name], data[name])
                if not complete_page_shape:
                    self.assertFalse({"returned", "has_more", "next_offset"} & view["data"].keys())
                entry = next(row for row in view["truncation"] if row["path"] == "/data/items/0/description")
                self.assertEqual(entry["total"], 10000)
                prefix = view["data"]["items"][0]["description"]
                tail = (await read(entry["path"], entry["next_offset"], 10))["data"]["items"]
                self.assertEqual(prefix + tail, "D" * (len(prefix) + 10))
            self.assertEqual((await read("/data/items/0/description", 0, 10))["data"]["items"], "D" * 10)
        for complete_page_shape in (False, True):
            with self.subTest(complete_page_shape=complete_page_shape):
                asyncio.run(run(complete_page_shape))

    def test_business_truncation_values_are_frozen_not_protocol(self):
        valid = {"path": "/chapter", "kind": "string", "total": 100, "offset": 3,
                 "returned": 10, "has_more": True, "next_offset": 13}
        values = [None, [{"path": "chapter"}], {"chapter": 1}, "chapter", 73,
                  [dict(valid, kind="unknown")], [dict(valid, total=True)],
                  [dict(valid, returned=-1)], [dict(valid, next_offset=99)],
                  [dict(valid, has_more="yes")], [dict(valid, scope="unknown")], [valid]]
        async def run(value):
            source = {"truncation": value, "data": {"description": "D" * 10000}}
            observed, read = await self.snapshot_reader(source, raw=True)
            self.assertIn("result_handle", observed)
            message = ModelMessage(role="tool", tool_name="library.inventory", content=json.dumps(observed))
            for maximum in (1200, 800, 600):
                message = _compact_chain([message], max_tool_chars=maximum)[0]
                view = json.loads(message.content)
                for path in ("/truncation", "/data/description"):
                    self.assertTrue(any(row["path"] == path or row.get("scope") == "subtree" and
                                        (not row["path"] or path.startswith(row["path"] + "/"))
                                        for row in view["truncation"]), path)
                self.assertFalse(any(row["path"] == "/chapter" for row in view["truncation"]))
            page = (await read("/truncation"))["data"]
            expected = [{"key": k, "value": v} for k, v in value.items()] if isinstance(value, dict) else value
            self.assertEqual(page["items"], expected if isinstance(value, (dict, list, str)) else [expected])
            self.assertEqual((await read("/data/description", 9990, 10))["data"]["items"], "D" * 10)
        for value in values:
            with self.subTest(value=value):
                asyncio.run(run(value))

    async def assert_collision_root_count(self, *, history):
        # Positive regression for SPEC-4cdce79-01; both paths start at the original view.
        source = {"truncation": None, "data": {"description": "D" * 10000}}
        observed, read, retain = await self.snapshot_fixture(source, raw=True)
        frozen = {**source, "ok": True, "status": "success", "summary": "工具执行完成"}
        self.assertEqual({row["path"] for row in observed["truncation"]}, {"/truncation", "/data/description"})
        oracle = (await read("", 0, 1))["data"]
        self.assertEqual(oracle["total"], len(frozen))
        message = ModelMessage(role="tool", tool_name="library.inventory", content=json.dumps(observed, ensure_ascii=False))
        if history:
            # No model-chain compression before this real retention/commit/reopen path.
            message = await retain(message, maximum_bytes=900)
        else:
            message = _compact_chain([message], max_tool_chars=600)[0]
        self.assertEqual(message.tool_name, "library.inventory")
        view = json.loads(message.content)
        self.assertEqual(view["result_handle"], observed["result_handle"])
        self.assertEqual((await read("/truncation"))["data"]["items"], [None])
        self.assertEqual((await read("/data/description", 9990, 10))["data"]["items"], "D" * 10)
        root = next(row for row in view["truncation"] if row["path"] == "")
        self.assertEqual(root["scope"], "subtree")
        self.assertEqual(root["total"], oracle["total"])
        self.assertEqual(root["returned"], len(view.keys() - {"truncation", "truncated", "read_tool", "read_method", "result_handle"}))
        self.assertLess(root["returned"], root["total"])

    def test_collision_root_count_survives_model_chain(self):
        asyncio.run(self.assert_collision_root_count(history=False))

    def test_collision_root_count_survives_direct_sqlite_history(self):
        asyncio.run(self.assert_collision_root_count(history=True))

    async def assert_frozen_coverage(self, view, frozen, read, handle):
        self.assertEqual(view["result_handle"], handle)
        self.assertTrue(view["truncation"])
        for row in view["truncation"]:
            node = frozen
            for part in row["path"].split("/")[1:]:
                key = part.replace("~1", "/").replace("~0", "~")
                node = node[int(key)] if isinstance(node, list) else node[key]
            total = len(node) if isinstance(node, (str, list, dict)) else 1
            self.assertEqual(row["total"], total, row["path"])
            oracle = (await read(row["path"], 0, 1))["data"]
            self.assertEqual(oracle["total"], total)

    def test_collision_members_count_once_regardless_of_value_shape(self):
        keys = ("truncation", "truncated", "result_handle", "read_tool")
        values = (None, 73, [1, 2, 3], {"a": 1, "b": 2})
        cases = [{key: value} for key in keys for value in values]
        cases.extend(({"truncation": None, "read_tool": {"a": 1, "b": 2}}, dict(zip(keys, values))))

        async def run(collisions):
            source = {**collisions, "data": {"description": "D" * 10000}}
            frozen = {**source, "ok": True, "status": "success", "summary": "工具执行完成"}
            observed, read, retain = await self.snapshot_fixture(source, raw=True)
            handle = observed["result_handle"]
            await self.assert_frozen_coverage(observed, frozen, read, handle)
            # The reader's nonzero page has one returned member, not the source total.
            oracle = (await read("", 1, 1))["data"]
            self.assertEqual((oracle["offset"], oracle["returned"], oracle["total"]), (1, 1, len(frozen)))
            original = ModelMessage(role="tool", tool_name="library.inventory", content=json.dumps(observed, ensure_ascii=False))
            message = original
            for maximum in (2000, 1200, 800, 800, 600, 600, 400, 400):
                message = _compact_chain([message], max_tool_chars=maximum)[0]
                self.assertLessEqual(len(message.content), maximum)
                view = json.loads(message.content)
                await self.assert_frozen_coverage(view, frozen, read, handle)
                # 800 can retain multiple obligations; do not assume a root merge.
                for key in collisions:
                    self.assertTrue(any(row["path"] == "/" + key or row["path"] == "" and
                                        row.get("scope") == "subtree" for row in view["truncation"]))
                if maximum <= 600:
                    self.assertEqual(view["truncation"][0]["path"], "")
                    self.assertEqual(view["truncation"][0]["total"], len(frozen))
            # Independent direct history path, then compress and persist it repeatedly.
            message = await retain(original)
            for _ in range(2):
                await self.assert_frozen_coverage(json.loads(message.content), frozen, read, handle)
                message = await retain(_compact_chain([message], max_tool_chars=600)[0])
                self.assertEqual(message.tool_name, "library.inventory")
            await self.assert_frozen_coverage(json.loads(message.content), frozen, read, handle)
            for key, value in collisions.items():
                expected = [{"key": k, "value": v} for k, v in value.items()] if isinstance(value, dict) else value
                self.assertEqual((await read("/" + key))["data"]["items"],
                                 expected if isinstance(value, (dict, list, str)) else [expected])
        for collisions in cases:
            with self.subTest(collisions=collisions):
                asyncio.run(run(collisions))

    def test_collision_root_count_deduplicates_escaped_ancestor_coverage(self):
        async def run():
            nested = {"first": 0, "~1/": "D" * 10000, **{str(n): n for n in range(600)}}
            source = {"truncation": None, "~/": nested}
            frozen = {**source, "ok": True, "status": "success", "summary": "工具执行完成"}
            observed, read, retain = await self.snapshot_fixture(source, raw=True)
            ancestor = next(row for row in observed["truncation"] if row["path"] == "/~0~1")
            self.assertEqual((ancestor["total"], ancestor["scope"]), (len(nested), "subtree"))
            self.assertLess(ancestor["returned"], ancestor["total"])
            self.assertEqual({row["path"] for row in observed["truncation"]}, {"/truncation", "/~0~1"})
            message = ModelMessage(role="tool", tool_name="library.inventory", content=json.dumps(observed, ensure_ascii=False))
            for maximum in (2000, 1200, 800, 800, 600, 600, 400):
                message = _compact_chain([message], max_tool_chars=maximum)[0]
                await self.assert_frozen_coverage(json.loads(message.content), frozen, read, observed["result_handle"])
            retained = await retain(ModelMessage(role="tool", tool_name="library.inventory", content=json.dumps(observed)))
            await self.assert_frozen_coverage(json.loads(retained.content), frozen, read, observed["result_handle"])
            # A true reader wrapper still counts source members and preserves its offset.
            page = await read("/~0~1", 1, 1, history_budgets=(1200, 800))
            self.assertEqual((page["data"]["offset"], page["data"]["returned"], page["data"]["total"]), (1, 1, len(nested)))
            self.assertEqual(page["data"]["items"][0]["key"], "~1/")
            await self.assert_frozen_coverage(page, frozen, read, observed["result_handle"])
            self.assertEqual((await read("/~0~1/~01~1", 9990, 10))["data"]["items"], "D" * 10)
            self.assertEqual((await read("/truncation"))["data"]["items"], [None])
        asyncio.run(run())

    def test_invalid_trusted_coverage_fails_closed(self):
        original = json.loads(DefaultProjector(max_model_chars=2000).project({"data": {"description": "D" * 10000}}).model_content)
        valid = original["truncation"][0]
        values = [None, {}, "row", [None], [{"path": "/data"}],
                  [dict(valid, path="chapter")], [dict(valid, path="/~2")],
                  [dict(valid, kind="unknown")], [dict(valid, total=True)],
                  [dict(valid, offset=-1)], [dict(valid, returned=10001)],
                  [dict(valid, next_offset=0)], [dict(valid, has_more=False)],
                  [dict(valid, scope="unknown")], [dict(valid, scope="subtree")]]
        for value in values:
            for maximum in (800, 24000):
                with self.subTest(value=value, maximum=maximum):
                    payload = dict(original, truncation=value)
                    message = ModelMessage(role="tool", tool_name="library.inventory", content=json.dumps(payload))
                    with self.assertRaises(ToolPipelineError) as caught:
                        _compact_chain([message], max_tool_chars=maximum)
                    self.assertEqual(caught.exception.code, "invalid_tool_result")

    def test_complete_business_protocol_collision_keeps_original_coordinates(self):
        async def run():
            row = {"path": "/chapter", "kind": "string", "total": 100, "offset": 3,
                   "returned": 10, "has_more": True, "next_offset": 13}
            source = {"truncation": [row], "truncated": True, "read_tool": "agent.read_result",
                      "result_handle": "business-handle", "data": {"description": "business"}}
            observed, read = await self.snapshot_reader(source, raw=True)
            self.assertNotEqual(observed["result_handle"], "business-handle")
            for key in ("truncation", "truncated", "read_tool", "result_handle"):
                self.assertTrue(any(entry["path"] == "/" + key or entry["path"] == "" and
                                    entry.get("scope") == "subtree" for entry in observed["truncation"]))
                data = (await read("/" + key))["data"]
                self.assertEqual(data["items"], [True] if key == "truncated" else source[key])
            self.assertEqual((await read("/data/description"))["data"]["items"], "business")
        asyncio.run(run())

    def test_reader_protocol_schema_is_complete_and_consistent(self):
        async def run():
            _, read = await self.snapshot_reader({"text": "D" * 10000})
            page = await read("/data/text", 71, 1000)
            full = page["data"]
            invalid = [None, {}, dict(full, kind="unknown"), dict(full, path="/~2"),
                       dict(full, total=True), dict(full, returned=True), dict(full, offset=-1),
                       dict(full, next_offset=None), dict(full, has_more=1), dict(full, items={}),
                       dict(full, items=[], returned=0, next_offset=71)]
            last = (await read("/data/text", 9990, 10))["data"]
            invalid.append({key: value for key, value in last.items() if key != "next_offset"})
            cases: list[dict[str, Any]] = [dict(page, data=value) for value in invalid]
            cases.extend({key: value for key, value in page.items() if key != missing} for missing in ("result_handle", "read_tool"))
            coverage = {key: full[key] for key in ("path", "kind", "total", "offset", "returned", "next_offset", "has_more")}
            cases.extend(dict(page, truncated=True, truncation=[dict(coverage, **change)]) for change in (
                {"total": 10001}, {"offset": 72, "next_offset": 1072}, {"returned": 999, "next_offset": 1070}, {"path": "/other"}))
            for payload in cases:
                with self.subTest(payload=payload):
                    message = ModelMessage(role="tool", tool_name="agent.read_result", content=json.dumps(dict(payload, summary="x" * 2000)))
                    with self.assertRaises(ToolPipelineError) as caught:
                        _compact_chain([message], max_tool_chars=800)
                    self.assertEqual(caught.exception.code, "invalid_tool_result")
        asyncio.run(run())

    def test_reader_coverage_survives_model_and_sqlite_history_budgets(self):
        async def run():
            text = "雪😀\"\\\n" * 2000
            records = [{"id": n, "description": str(n) + text} for n in range(8)]
            _, read = await self.snapshot_reader({"text": text, "items": records})
            for path, offset, limit, total in (("/data/text", 71, 1000, len(text)), ("/data/items", 3, 1, 8)):
                page = await read(path, offset, limit, history_budgets=(1200, 800, 600), storage_bytes=900)
                data = page["data"]
                self.assertEqual((data["path"], data["offset"], data["total"]), (path, offset, total))
                self.assertEqual(data["returned"], len(data["items"]))
                self.assertTrue(page["truncation"])
                for row in page["truncation"]:
                    self.assertTrue(row["has_more"])
                    self.assertTrue(row["path"] == path or row["path"].startswith(path + "/3"))
                    if row["path"] == path:
                        self.assertEqual((row["offset"], row["total"]), (offset, total))
                if path.endswith("text"):
                    self.assertEqual(data["items"], text[offset:offset + data["returned"]])
                    self.assertEqual(data["next_offset"], offset + data["returned"])
                else:
                    child = (await read("/data/items/3/description", 0, 10))["data"]["items"]
                    self.assertEqual(child, records[3]["description"][:10])
        asyncio.run(run())

    def test_nonzero_page_truncation_points_to_frozen_snapshot(self):
        async def run():
            text = "SECOND_ROW😀\"\\\n" * 900
            _, read = await self.snapshot_reader({
                "items": [{"description": "FIRST_ROW"}, {"description": text}],
                "map": {"first": 1, "": {"~/雪": text}},
                "text": text, "cookie": "secret-not-for-model", "path": "/srv/private/file",
            })
            for source, expected in (("/data/items", "/data/items/1/description"),
                                     ("/data/map", "/data/map//~0~1雪")):
                page = await read(source, 1, 1)
                entry = next(row for row in page["truncation"] if row["kind"] == "string")
                self.assertEqual(entry["path"], expected)
                leaf = await read(entry["path"], 0, 12)
                self.assertEqual(leaf["data"]["items"], text[:12])
                first = page["data"]["items"][0]
                received = first["description"] if source == "/data/items" else first["value"]["~/雪"]
                self.assertEqual(entry["returned"], len(received))
                self.assertEqual(entry["total"], len(text))
                offset = entry["next_offset"]
                while True:
                    tail = (await read(entry["path"], offset))["data"]
                    received += tail["items"]
                    if not tail["has_more"]:
                        break
                    self.assertGreater(tail["next_offset"], offset)
                    offset = tail["next_offset"]
                self.assertEqual(received, text)
            # Unicode and JSON escapes use Python character coordinates, not encoded bytes.
            offset, received = 7, ""
            while True:
                page = await read("/data/text", offset)
                data = page["data"]
                self.assertEqual(data["returned"], len(data["items"]))
                received += data["items"]
                if not data["has_more"]:
                    break
                self.assertGreater(data["next_offset"], offset)
                offset = data["next_offset"]
            self.assertEqual(received, text[7:])
            self.assertIn("已隐藏", (await read("/data/cookie"))["data"]["items"])
            self.assertIn("路径已隐藏", (await read("/data/path"))["data"]["items"])
        asyncio.run(run())

    def test_short_dictionary_fields_have_explicit_recoverable_coverage(self):
        async def run():
            fields = {f"field-{n}": n for n in range(600)}
            observed, read = await self.snapshot_reader({"fields": fields})
            entry = next(row for row in observed.get("truncation", []) if row["path"] == "/data/fields")
            self.assertEqual(entry["total"], 600)
            self.assertEqual(entry["returned"], len(observed["data"]["fields"]))
            self.assertTrue(entry["has_more"])
            received = dict(observed["data"]["fields"])
            offset = entry["next_offset"]
            while True:
                data = (await read(entry["path"], offset))["data"]
                self.assertEqual(data["returned"], len(data["items"]))
                received.update((row["key"], row["value"]) for row in data["items"])
                if not data["has_more"]:
                    break
                self.assertGreater(data["next_offset"], offset)
                offset = data["next_offset"]
            self.assertEqual(received, fields)
        asyncio.run(run())

    def test_all_truncated_leaves_remain_covered_after_recompression(self):
        records = [{"id": n, "description": f"ROW-{n}-" + "详情" * 3000} for n in range(8)]
        content = json.dumps({"data": {"items": records}}, ensure_ascii=False)
        for maximum in (4500, 2000, 1200, 800, 400):
            content = compact_tool_content(content, maximum=maximum, trusted=maximum != 4500)
            self.assertLessEqual(len(content), maximum)
            result = json.loads(content)
            coverage = result.get("truncation", [])
            for index in range(8):
                path = f"/data/items/{index}/description"
                self.assertTrue(any(row["path"] == path or row.get("scope") == "subtree" and
                                    (not row["path"] or path.startswith(row["path"] + "/")) for row in coverage), path)
            self.assertTrue(all(row["has_more"] and row["next_offset"] is not None for row in coverage))
            self.assertLessEqual(len(coverage), 4)

    def test_subtree_coverage_can_be_expanded_without_requery(self):
        async def run():
            records = [{"id": n, "nested": {"": {"~/雪": f"ROW-{n}-" + "详情😀\"\\\n" * 1000}},
                        "description": f"DESCRIPTION-{n}-" + "说明" * 1500} for n in range(8)]
            observed, read = await self.snapshot_reader({"items": records})
            self.assertTrue(any(row.get("scope") == "subtree" for row in observed["truncation"]))
            requests = 0

            async def complete(path):
                nonlocal requests
                offset, values, text = 0, [], ""
                while True:
                    requests += 1
                    self.assertLess(requests, 500)
                    page = await read(path, offset, 1 if path == "/data/items" else 1000)
                    data = page["data"]
                    self.assertEqual(data["returned"], len(data["items"]))
                    if data["kind"] == "str":
                        text += data["items"]
                    elif data["kind"] == "dict":
                        for row in data["items"]:
                            child = path + "/" + row["key"].replace("~", "~0").replace("/", "~1")
                            values.append((row["key"], await complete(child) if page.get("truncated") else row["value"]))
                    else:
                        for index, row in enumerate(data["items"], offset):
                            values.append(await complete(path + "/" + str(index)) if page.get("truncated") else row)
                    if not data["has_more"]:
                        break
                    self.assertGreater(data["next_offset"], offset)
                    offset = data["next_offset"]
                return text if data["kind"] == "str" else dict(values) if data["kind"] == "dict" else values if data["kind"] == "list" else values[0]

            self.assertEqual(await complete("/data/items"), records)
        asyncio.run(run())

    def test_budget_matrix_preserves_original_totals_and_page_ranges(self):
        sources = [
            {"ok": True, "status": "success", "data": {"k" * 700: 1}},
            {"data": {"fields": {str(n): n for n in range(600)}}},
            {"data": {"items": [{"id": n, "description": "😀\"\\\n" * 1000} for n in range(8)]}},
        ]
        for source in sources:
            content = json.dumps(source, ensure_ascii=False)
            for maximum in (24000, 4500, 2000, 1200, 800, 400):
                content = compact_tool_content(content, maximum=maximum, trusted=maximum != 24000)
                self.assertLessEqual(len(content), maximum)
                for row in json.loads(content).get("truncation", []):
                    node: Any = source
                    for part in row["path"].split("/")[1:]:
                        key = part.replace("~1", "/").replace("~0", "~")
                        node = node[int(key)] if isinstance(node, list) else node[key]
                    self.assertEqual(row["total"], len(node) if isinstance(node, (str, list, dict)) else 1)
        text = "前缀😀\"\\\n" * 1000
        content = json.dumps({"result_handle": "ref_example", "read_tool": "agent.read_result", "data": {
            "path": "/data/text", "kind": "str", "offset": 71, "items": text[71:2071], "total": len(text),
            "returned": 2000, "has_more": True, "next_offset": 2071}}, ensure_ascii=False)
        for maximum in (2000, 1200, 800, 400):
            content = compact_tool_content(content, maximum=maximum, result_page=True, trusted=True)
            self.assertLessEqual(len(content), maximum)
            data = json.loads(content)["data"]
            self.assertEqual(data["returned"], len(data["items"]))
            self.assertEqual(data["items"], text[data["offset"]:data["next_offset"]])
            for row in json.loads(content)["truncation"]:
                self.assertEqual(row["path"], "/data/text")
                self.assertEqual(row["total"], len(text))
                self.assertEqual(row["returned"], data["returned"])

    def test_pointer_validation_and_empty_page_boundaries(self):
        async def run():
            _, read = await self.snapshot_reader({"~2": "literal", "": {"": "empty-key"}, "items": [1],
                                                 "empty": {}, "text": "", "padding": "x" * 5000})
            self.assertEqual((await read("/data//"))["data"]["items"], "empty-key")
            for path in ("/data/~2", "/data/items/01", "/data/items/-1", "/data/missing", "/data/items/0/child"):
                with self.assertRaises(ToolPipelineError) as invalid:
                    await read(path)
                self.assertEqual(invalid.exception.code, "invalid_arguments")
            for path, total in (("/data/items", 1), ("/data/empty", 0), ("/data/text", 0)):
                for offset in (total, total + 10):
                    data = (await read(path, offset))["data"]
                    self.assertEqual(data["returned"], 0)
                    self.assertFalse(data["has_more"])
                    self.assertIsNone(data["next_offset"])
        asyncio.run(run())

    def test_unpageable_key_fails_explicitly_instead_of_zero_progress(self):
        async def run():
            _, read = await self.snapshot_reader({"fields": {"巨键" * 2000: 1}})
            with self.assertRaises(ToolPipelineError) as blocked:
                await read("/data/fields", 0, 1)
            self.assertEqual(blocked.exception.code, "context_budget_exceeded")
        asyncio.run(run())

    def test_long_pointer_uses_readable_ancestor_or_explicit_boundary(self):
        raw = json.dumps({"data": {"fields": {"键" * 1100: "内容" * 20000}}}, ensure_ascii=False)
        view = json.loads(compact_tool_content(raw, maximum=24000))
        self.assertTrue(all(len(row["path"]) <= 1000 for row in view["truncation"]))
        async def run():
            observed, read = await self.snapshot_reader({"fields": {"键" * 1100: "内容" * 4000}})
            self.assertTrue(all(len(row["path"]) <= 1000 for row in observed["truncation"]))
            with self.assertRaises(ToolPipelineError) as blocked:
                await read("/data/fields", 0, 1)
            self.assertEqual(blocked.exception.code, "context_budget_exceeded")
        asyncio.run(run())

    def test_small_fact_survives_large_result_compression(self):
        fact = ModelMessage(role="tool", content='{"ok":true,"data":{"important_fact":"KEEP_ME"}}')
        large = replace(fact, content=json.dumps({"data": {"items": ["large" * 2000] * 40}}))
        compacted = _compact_chain([fact, large], max_tool_chars=400)
        self.assertIs(compacted[0], fact)
        self.assertLessEqual(len(compacted[1].content), 400)
        self.assertTrue(json.loads(compacted[1].content)["truncated"])

    def test_long_overviews_are_trimmed_before_candidate_identity_fields(self):
        records = [{"tmdb_id": str(i), "year": 2010+i, "region": "CN", "overview": "冗长描述"*500} for i in range(20)]
        result = json.loads(compact_tool_content(json.dumps({"data": {"items": records, "total": 20}}, ensure_ascii=False), maximum=4500))
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["data"]["items"]), 20)
        self.assertEqual([(item["tmdb_id"],item["year"],item["region"]) for item in result["data"]["items"]],
                         [(item["tmdb_id"],item["year"],item["region"]) for item in records])
        # Many leaf records coalesce into an explicit ancestor, not an unreported [:4].
        self.assertTrue(all(item["kind"] == "string" or
                            item["scope"] == "subtree" and item["path"] == "/data/items" and
                            item["total"] == item["returned"] == 20
                            for item in result["truncation"]))

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

    def test_snapshots_never_register_a_narrow_reader(self):
        from app.agent.kernel.adapters import consume_events

        from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
        from app.agent.kernel.session import AgentSession
        from app.agent.kernel.state import InMemorySessionStateStore
        from app.agent.kernel.state_types import AgentInput
        from tests.test_agent_kernel_core import ScriptedModel

        async def run():
            tool = KernelToolSpec(name="library.inventory", domain="library", effect=ToolEffect.READ,
                description="读取库存", input_schema={"type": "object", "properties": {}},
                read=lambda *_: {"data": {"items": [{"title": "x"*1000, "id": i} for i in range(100)]}})
            catalog, state = ToolCatalog([tool]), InMemorySessionStateStore()
            model = ScriptedModel([[
                ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall("scan", tool.name, {})),
                ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")], [
                ModelEvent(ModelEventType.TEXT_DELTA, text="已取得快照，明细可继续读取"),
                ModelEvent(ModelEventType.FINISH, finish_reason="stop")]])
            pipeline = ToolPipeline(catalog=catalog, state_store=state, projector=DefaultProjector(max_model_chars=2000))
            session = AgentSession(model=model, catalog=catalog,  pipeline=pipeline, state_store=state)
            result = await consume_events(session.run(AgentInput(owner="owner", session_id="paging", message="读取库存")))
            self.assertEqual(result.status, "success")
            self.assertNotIn("agent__read_result", [tool["name"] for tool in model.requests[0].tools])
            self.assertNotIn("agent__read_result", [tool["name"] for tool in model.requests[1].tools])
            self.assertIn("tool_result", (await state.load(owner="owner", session_id="paging")).ref_kinds)
        asyncio.run(run())

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
            self.assertEqual(view["read_method"], "session.result")
            handle = view["result_handle"]
            # 重建存储和Pipeline后继续读取同一加密快照，不重复外部查询。
            restored = SQLiteKernelStore(secret_provider=lambda: "result-paging-test")
            reader = ToolPipeline(catalog=ToolCatalog([]), state_store=restored, reference_store=restored,
                                  projector=DefaultProjector(max_model_chars=24000))
            payload = await SessionService(reader, context).result(handle, "/data/items")
            self.assertEqual(payload["data"], data)
            self.assertNotIn("secret-not-for-model", json.dumps(payload))
            with self.assertRaises(ToolPipelineError) as missing:
                await SessionService(reader, context).result(handle, "/data/missing")
            self.assertEqual(missing.exception.code, "invalid_arguments")
            for other in (replace(context, owner="other"), replace(context, session_id="other")):
                with self.assertRaises(ToolPipelineError) as caught:
                    await SessionService(reader, other).result(handle, "/data/items")
                self.assertEqual(caught.exception.code, "reference_invalid")
            wrong_kind = await restored.put(owner="owner", session_id="session", kind="resource_candidates", value={"parts": []}, ttl_seconds=900)
            with self.assertRaises(ToolPipelineError) as wrong_type:
                await SessionService(reader, context).result(wrong_kind.ref)
            self.assertEqual(wrong_type.exception.code, "reference_invalid")
            restored._clock = lambda: 10**12
            with self.assertRaises(ToolPipelineError) as expired:
                await SessionService(reader, context).result(handle, "/data/items")
            self.assertEqual(expired.exception.code, "reference_invalid")
        asyncio.run(run())



    def test_batch_missing_episodes_survive_existing_paging_and_cold_restart(self):
        from app.modules.episode_audit import EpisodeAuditResult, check_library_updates

        expected = [{"season": season, "episode": episode}
                    for season in (1, 3) for episode in range(1, 151) if episode % 5]
        titles = [f"缺集完整性{i}" for i in range(20)]

        def audit(arguments, **_):
            return EpisodeAuditResult(True, "updates_available", "已核对库存", data={
                "title": arguments["query"], "missing_count": len(expected),
                "missing_sample": expected, "missing_sample_truncated": False,
            })

        async def run():
            with patch("app.modules.episode_audit.audit_series_episodes", side_effect=audit):
                result = check_library_updates({"queries": titles, "as_of": "2026-10-14"})
            observed, read = await self.snapshot_reader(result, raw=True)
            self.assertIn("result_handle", observed)
            for index, title in enumerate(titles):
                restored, offset = [], 0
                for _ in range(len(expected)):
                    page = await read(f"/data/items/{index}/missing_sample", offset=offset)
                    data = page["data"]
                    self.assertEqual(data["total"], len(expected))
                    restored.extend(data["items"])
                    if data["next_offset"] is None:
                        break
                    self.assertGreater(data["next_offset"], offset)
                    offset = data["next_offset"]
                self.assertEqual(restored, expected, title)
            self.assertEqual(result["data"]["counts"]["requested"], 20)
        asyncio.run(run())
