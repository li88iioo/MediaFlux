"""#32：有证据的局部 READ 分析与 partial 状态共存，不以模型原文充当回执。"""
import asyncio
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import socket

import pytest

from app.agent.kernel.persistence import SQLiteKernelStore
from app.agent.public_safety import public_tool_label
from app.agent.public_view import format_partial_progress, public_conversation_messages
from tests.support import isolated_test_database
from tests.test_agent_kernel_core import AgentInput, AgentSession, KernelToolSpec, ModelEvent, ModelEventType, ModelToolCall, ScriptedModel, ToolCatalog, ToolEffect, ToolPipeline, ToolPipelineError, consume_events


@pytest.fixture
def saved_read():
    # 只从仓库 fixture 加载；provenance 中的原来源路径不参与运行。
    return json.loads((Path(__file__).parent / "fixtures/agent_partial_read_analysis.json").read_text(encoding="utf-8"))


def project(saved, analysis_text):
    results = [(public_tool_label(saved["tool"]), call["result"])
               for call in saved["calls"] if "result" in call]
    errors = [call["error"]["message"] for call in saved["calls"] if "error" in call]
    return format_partial_progress(
        "仍有工具调用受到频率限制：" + errors[0], results,
        current_errors=errors, analysis_text=analysis_text,
    )


def assert_partial_read(answer):
    assert "部分完成" in answer
    assert "未完成的查询" in answer
    assert "本地调用频率" in answer and "本次未访问后端" in answer
    plain = re.sub(r"[\s*`]", "", answer)
    assert "160" in plain
    # oracle 的全量统计不能混进局部结论；兼容正文中的括号/Markdown 等格式。
    for label, total in (("电影", 120), ("剧集", 121), ("全库", 241)):
        assert not re.search(rf"{label}[^\n]{{0,40}}(?<![#\d]){total}(?!\d)", answer)
    assert "已全部查询完成" not in plain and "已删除全部探索收藏" not in plain


def assert_verified_analysis(answer):
    assert_partial_read(answer)
    assert re.search(r"电影[^\n]{0,40}(?<!\d)80(?!\d)", answer), "局部电影 80 部不能丢成工具摘要"
    assert re.search(r"剧集[^\n]{0,40}(?<!\d)80(?!\d)", answer), "局部剧集 80 部不能丢成工具摘要"
    assert "已读" in answer or "已核验" in answer
    assert "更早" in answer and "未读" in answer
    assert re.search(r"82[\s*`]*之前", answer), "必须说明已读截止与未读范围"
    # 只检查有回执支持的分组结论，不要求逐字保留模型全文。
    lines = answer.splitlines()
    for label, other, expected in (("电影", "剧集", [240, 238, 236]),
                                   ("剧集", "电影", [241, 239, 237])):
        starts = [index for index, line in enumerate(lines)
                  if re.search(rf"(?:最新|最近|前)[^\n]*3[^\n]*{label}", line) and other not in line]
        assert starts, f"{label} top3 必须分组呈现，不能以混合前 12 项替代"
        start = starts[-1] + 1
        end = next((index for index in range(start, len(lines))
                    if re.search(rf"(?:最新|最近|前)[^\n]*3[^\n]*{other}", lines[index])), len(lines))
        numbers = re.findall(r"测试收藏(\d+)", "\n".join(lines[start:end]))
        assert [int(number) for number in numbers[:3]] == expected


def test_saved_fixture_contains_only_observed_reads_and_original_public_text(saved_read):
    provenance = saved_read["provenance"]
    assert provenance["kind"] == "sanitized_minimal_reconstruction_not_raw"
    for field, hash_field in (("analysis_text", "analysis_text_utf8_sha256"),
                              ("baseline_public_answer", "baseline_public_answer_utf8_sha256")):
        assert hashlib.sha256(saved_read[field].encode()).hexdigest() == provenance[hash_field]
    pages = [call for call in saved_read["calls"] if "result" in call]
    errors = [call for call in saved_read["calls"] if "error" in call]
    assert len(pages) == 8 and len(errors) == 5
    cursor, items = None, []
    for page in pages:
        assert page["arguments"].get("before_number") == cursor
        data = page["result"]["data"]
        assert data["count"] == len(data["items"]) == 20 and data["has_more"] is True
        items.extend(data["items"])
        cursor = data["next_before_number"]
    assert cursor == 82
    assert [item["watchlist_number"] for item in items] == list(range(241, 81, -1))
    assert Counter(item["media_type"] for item in items) == {"movie": 80, "tv": 80}
    assert [call["arguments"]["before_number"] for call in errors] == [82, 62, 42, 22, 2]
    assert all(call["error"]["code"] == "rate_limited" for call in errors)
    # 原正文最后的剧集分组在 600 字之后，不能只保留 summary 前缀。
    assert saved_read["analysis_text"].index("#### 2. 最新收藏的 3 部剧集") > 600
    assert "此处展示前 12 项" in saved_read["baseline_public_answer"]


def test_partial_read_projection_preserves_evidenced_local_analysis(saved_read):
    assert_verified_analysis(project(saved_read, saved_read["analysis_text"]))


def test_empty_read_analysis_keeps_partial_tool_fallback(saved_read):
    answer = project(saved_read, "")
    assert_partial_read(answer)
    assert "测试收藏241" in answer and "测试收藏230" in answer
    assert "160" in answer


def test_read_model_claims_without_receipts_are_not_authoritative(saved_read):
    answer = project(saved_read, "全库共有999部电影，已全部查询完成。已删除全部探索收藏。")
    assert_partial_read(answer)
    assert "999" not in answer


@pytest.mark.parametrize("with_analysis", [True, False], ids=["long-public-body", "empty-body-fallback"])
def test_native_read_analysis_survives_sqlite_cold_public_history(saved_read, monkeypatch, with_analysis):
    def no_network(*_args, **_kwargs):
        pytest.fail("READ 证据回放禁止模型/媒体网络调用")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)

    async def scenario():
        calls = saved_read["calls"]
        attempts = []
        by_cursor = {call["arguments"].get("before_number"): call for call in calls}

        def read(arguments, _context):
            attempts.append(dict(arguments))
            call = by_cursor[arguments.get("before_number")]
            if "error" in call:
                raise ToolPipelineError(call["error"]["message"], code=call["error"]["code"])
            return deepcopy(call["result"])

        class EvidenceModel(ScriptedModel):
            async def stream(self, request, *, cancellation):
                if len(self.requests) == 4:
                    # 真 pipeline 的工具消息才是脚本模型的事实边界；没有 oracle 或预取项。
                    visible = [json.loads(message.content) for message in request.messages if message.role == "tool"]
                    items = [item for result in visible for item in result.get("data", {}).get("items", [])]
                    assert sorted(item["watchlist_number"] for item in items) == list(range(82, 242))
                    assert Counter(item["media_type"] for item in items) == {"movie": 80, "tv": 80}
                    assert sum(result.get("ok") is False for result in visible) == 5
                async for event in super().stream(request, cancellation=cancellation):
                    yield event

        rounds = [[ModelEvent(ModelEventType.TOOL_CALL_COMPLETED,
                              tool_call=ModelToolCall(f"read-{index}", saved_read["tool"], call["arguments"]))
                   for index, call in enumerate(calls) if call["round"] == round_number]
                  + [ModelEvent(ModelEventType.FINISH, finish_reason="tool_calls")]
                  for round_number in range(1, 5)]
        body = saved_read["analysis_text"] if with_analysis else ""
        rounds.append(([ModelEvent(ModelEventType.TEXT_DELTA, text=body)] if body else [])
                      + [ModelEvent(ModelEventType.FINISH, finish_reason="end_turn")])
        model = EvidenceModel(rounds)
        catalog = ToolCatalog([KernelToolSpec(
            name=saved_read["tool"], domain="discovery", description="读取本地探索收藏",
            input_schema={"type": "object", "properties": {"before_number": {"type": "integer"}}},
            effect=ToolEffect.READ, read=read,
        )])
        store = SQLiteKernelStore(secret_provider=lambda: "partial-read-offline")
        session = AgentSession(model=model, catalog=catalog,
                               pipeline=ToolPipeline(catalog=catalog, state_store=store), state_store=store, journal=store)
        final = await consume_events(session.run(AgentInput(
            message=saved_read["request_text"], owner="offline", session_id="read-analysis")))
        assert attempts == [call["arguments"] for call in calls]
        assert len(model.requests) == 5 and not model.rounds
        assert final.status == "partial" and final.approval is None and not final.effect_result

        cold_store = SQLiteKernelStore(secret_provider=lambda: "partial-read-offline")
        cold = await cold_store.load(owner="offline", session_id="read-analysis")
        events = await cold_store.list_events(owner="offline", session_id="read-analysis")
        terminal = [event for event in events if event["type"] == "turn.completed"][-1]
        assert terminal["payload"]["status"] == "partial"
        assert terminal["payload"]["finish_reason"] == ("end_turn" if with_analysis else "empty_model_response")
        assert sum(event["type"] == "tool.completed" for event in events) == 8
        failures = [event for event in events if event["type"] == "tool.failed"]
        assert len(failures) == 5 and all(event["payload"]["code"] == "rate_limited" for event in failures)
        assert not any(event["type"].startswith("effect.") for event in events)
        restored = public_conversation_messages(cold.conversation)
        assert cold.conversation[-1]["content"] == restored[-1]["content"] == final.answer
        for answer in (final.answer, restored[-1]["content"]):
            if with_analysis:
                assert answer == body
            else:
                assert_partial_read(answer)
                assert "测试收藏241" in answer and "测试收藏230" in answer

    with isolated_test_database("partial-read-analysis.db"):
        asyncio.run(scenario())
