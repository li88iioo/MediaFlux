import unittest

from app.agent.presentation_stream import (
    PublicNarrativeProjector,
    PublicNarrativeValidationError,
    apply_streamed_answer,
    select_agent_answer_stream,
)


class AgentPresentationStreamTests(unittest.TestCase):
    def test_projector_publishes_safe_sentence_before_rejecting_unsafe_tail(self):
        projector = PublicNarrativeProjector()

        projected = projector.feed("检查已经完成。请访问 https://")

        self.assertIsNotNone(projected)
        assert projected is not None
        self.assertEqual(projected.delta, "检查已经完成。")
        self.assertEqual(projected.cumulative, "检查已经完成。")
        with self.assertRaises(PublicNarrativeValidationError):
            projector.raise_pending_error()
        self.assertEqual(projector.published_answer(), "检查已经完成。")

    def test_projector_finalizes_complete_safe_answer(self):
        projector = PublicNarrativeProjector()
        self.assertIsNone(projector.feed("下载队列"))
        projected = projector.feed("正常，共 3 项任务。")

        self.assertIsNotNone(projected)
        self.assertEqual(projector.finalize(), "下载队列正常，共 3 项任务。")

    def test_projector_preserves_safe_multiline_narrative(self):
        projector = PublicNarrativeProjector()
        projected = projector.feed(
            "为你整理了 2 部影片：\n\n- 《示例一》：已经上线。\n- 《示例二》：即将上映。"
        )

        self.assertIsNotNone(projected)
        self.assertEqual(
            projector.finalize(),
            "为你整理了 2 部影片：\n\n- 《示例一》：已经上线。\n- 《示例二》：即将上映。",
        )

    def test_confirmation_and_confirmed_action_never_select_provider_stream(self):
        def tool_factory(*_args, **_kwargs):
            return object()

        def conversation_factory(*_args, **_kwargs):
            return object()

        for mode in ("confirmation_required", "confirmed_action"):
            stream = select_agent_answer_stream(
                "执行操作",
                {
                    "mode": mode,
                    "tool_call": {"name": "config.update"},
                    "result": {"ok": True},
                },
                owner="owner",
                tool_stream_factory=tool_factory,
                conversation_stream_factory=conversation_factory,
            )
            self.assertIsNone(stream)


    def test_existing_presentation_skips_second_provider_stream(self):
        calls = []

        def tool_factory(*_args, **_kwargs):
            calls.append("tool")
            return object()

        def conversation_factory(*_args, **_kwargs):
            calls.append("conversation")
            return object()

        stream = select_agent_answer_stream(
            "检查订阅更新",
            {
                "mode": "read_plan",
                "tool_call": {"name": "agent.read_plan"},
                "result": {"ok": True, "status": "completed"},
                "presentation": {
                    "version": 1,
                    "source": "llm",
                    "kind": "narrative",
                    "narrative": "订阅和媒体库已经核对完成。",
                },
            },
            owner="owner",
            tool_stream_factory=tool_factory,
            conversation_stream_factory=conversation_factory,
        )

        self.assertIsNone(stream)
        self.assertEqual(calls, [])

    def test_conversation_stream_uses_conversation_factory_and_skips_partial(self):
        calls = []

        def tool_factory(*_args, **_kwargs):
            calls.append("tool")
            return object()

        def conversation_factory(*_args, **_kwargs):
            calls.append("conversation")
            return "conversation-stream"

        response = {
            "mode": "conversation",
            "result": {"ok": True, "status": "answer", "summary": "旧摘要"},
        }
        stream = select_agent_answer_stream(
            "继续说明",
            response,
            owner="owner",
            tool_stream_factory=tool_factory,
            conversation_stream_factory=conversation_factory,
        )
        partial = select_agent_answer_stream(
            "继续说明",
            {
                "mode": "conversation",
                "result": {"ok": False, "status": "partial", "summary": "部分结果"},
            },
            owner="owner",
            tool_stream_factory=tool_factory,
            conversation_stream_factory=conversation_factory,
        )

        self.assertEqual(stream, "conversation-stream")
        self.assertIsNone(partial)
        self.assertEqual(calls, ["conversation"])

    def test_conversation_stream_refreshes_result_and_display_projection(self):
        response = {
            "mode": "conversation",
            "result": {"ok": True, "status": "answer", "summary": "旧摘要"},
            "display": {"summary": "旧展示"},
        }

        presented = apply_streamed_answer(
            response,
            "新的自然语言回答。",
            result_projector=lambda result: {
                "summary": result["summary"],
                "status": "projected",
            },
        )

        self.assertEqual(presented["result"]["summary"], "新的自然语言回答。")
        self.assertEqual(presented["display"], {
            "summary": "新的自然语言回答。",
            "status": "projected",
        })
        self.assertEqual(response["result"]["summary"], "旧摘要")
        self.assertEqual(response["display"]["summary"], "旧展示")

    def test_streamed_tool_answer_preserves_structured_result_and_confirmation(self):
        response = {
            "mode": "answer",
            "tool_call": {"name": "downloads.diagnose_queue", "arguments": {}},
            "result": {"ok": True, "summary": "确定性摘要", "items": [1]},
            "confirmation": {"confirmation_id": "ticket"},
        }

        presented = apply_streamed_answer(response, "自然语言说明。")

        self.assertEqual(presented["result"], response["result"])
        self.assertEqual(presented["confirmation"], response["confirmation"])
        self.assertEqual(presented["presentation"]["narrative"], "自然语言说明。")
        self.assertEqual(response["result"]["summary"], "确定性摘要")


if __name__ == "__main__":
    unittest.main()
