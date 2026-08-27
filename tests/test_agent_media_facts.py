"""结构化媒体事实与跨轮集数换算测试。"""
from __future__ import annotations

import unittest

from app.agent.media_facts import (
    absolute_episode_answer,
    derive_media_facts,
    media_facts_match_context,
    validate_media_facts,
)


class AgentMediaFactsTests(unittest.TestCase):
    def test_web_answer_and_indexer_evidence_are_stored_in_separate_fact_scopes(self):
        answer = (
            "优酷官方目前更新情况如下：\n\n"
            "- 当前季播：第三季（年番）正片已更新至第 22 集"
            "（2026 年 8 月 22 日最新更新）\n"
            "- 第一季：共 26 集（已完结）\n"
            "- 第二季：共 35 集（已完结）\n"
            "- 全系列累计第 83 集"
        )
        executions = [
            {
                "tool_name": "web.search",
                "arguments": {"query": "沧元图 优酷 官方更新"},
                "response": {
                    "result": {
                        "ok": True,
                        "data": {"results": [{
                            "title": "沧元图第三季年番",
                            "source": "v.youku.com",
                            "snippet": (
                                "正片已更新至第22集；第一季共26集；"
                                "第二季共35集；全系列累计第83集。"
                            ),
                            "published_date": "2026-08-22",
                        }]},
                    }
                },
            },
            {
                "tool_name": "indexer.search_resources",
                "arguments": {"title": "沧元图"},
                "response": {
                    "result": {
                        "ok": True,
                        "data": {"items": [{
                            "title": "[GM-Team][沧元图 第3季][2026][22][GB][4K]"
                        }]},
                    }
                },
            },
        ]

        facts = derive_media_facts(
            message="沧元图 官方更新到多集啦？",
            answer=answer,
            executions=executions,
        )

        self.assertEqual(facts["identity"]["title"], "沧元图")
        self.assertEqual(facts["official_progress"], {
            "season": 3,
            "episode": 22,
            "absolute_episode": 83,
            "as_of": "2026-08-22",
            "source": "web",
        })
        self.assertEqual(facts["indexer_progress"], {
            "season": 3,
            "episode": 22,
            "source": "indexer",
        })
        self.assertEqual(
            [(item["season"], item["episodes"]) for item in facts["season_counts"]],
            [(1, 26), (2, 35)],
        )

    def test_llm_narrative_without_web_evidence_is_not_persisted_as_official_fact(self):
        facts = derive_media_facts(
            message="沧元图官方更新到哪里了？",
            answer=(
                "优酷官方第三季正片已更新至第 99 集，"
                "第一季共 40 集。"
            ),
            executions=[{
                "tool_name": "web.search",
                "arguments": {"query": "沧元图 优酷 官方更新"},
                "response": {
                    "result": {
                        "ok": True,
                        "data": {"results": []},
                    }
                },
            }],
        )

        self.assertEqual(facts["identity"]["title"], "沧元图")
        self.assertNotIn("official_progress", facts)
        self.assertNotIn("season_counts", facts)

    def test_non_official_web_snippet_is_not_persisted_as_official_fact(self):
        facts = derive_media_facts(
            message="沧元图官方更新到哪里了？",
            answer="第三方页面称正片已更新至第 22 集。",
            executions=[{
                "tool_name": "web.search",
                "arguments": {"query": "沧元图 优酷 官方更新"},
                "response": {
                    "result": {
                        "ok": True,
                        "data": {"results": [{
                            "title": "沧元图第三季更新进度",
                            "source": "example.com",
                            "snippet": "优酷官方第三季正片已更新至第22集。",
                        }]},
                    }
                },
            }],
        )

        self.assertNotIn("official_progress", facts)

    def test_indexer_title_alone_never_becomes_official_progress(self):
        facts = derive_media_facts(
            message="沧元图更新到哪里了",
            answer="资源索引找到第三季第 22 集。",
            executions=[{
                "tool_name": "indexer.search_resources",
                "arguments": {"title": "沧元图"},
                "response": {"result": {"ok": True, "data": {"items": [{
                    "title": "沧元图 第3季 [22]"
                }]}}},
            }],
        )

        self.assertNotIn("official_progress", facts)
        self.assertEqual(facts["indexer_progress"]["episode"], 22)

    def test_absolute_episode_uses_only_complete_prior_season_counts(self):
        facts = {
            "version": 1,
            "identity": {"title": "沧元图", "media_type": "anime"},
            "season_counts": [
                {"season": 1, "episodes": 26, "source": "web"},
                {"season": 2, "episodes": 35, "source": "web"},
            ],
        }
        answer = absolute_episode_answer(
            "第三季第22集是多少集？",
            [{"role": "assistant", "media_facts": facts}],
        )

        self.assertIn("累计第 83 集", answer)
        self.assertIn("26 + 35 + 22 = 83", answer)
        self.assertEqual(
            absolute_episode_answer(
                "第四季第1集是多少集？",
                [{"role": "assistant", "media_facts": facts}],
            ),
            "",
        )

    def test_new_identity_does_not_inherit_previous_title_facts(self):
        previous = {
            "version": 1,
            "identity": {"title": "沧元图"},
            "season_counts": [{"season": 1, "episodes": 26, "source": "web"}],
        }
        facts = derive_media_facts(
            message="牧神记官方更新到哪里了？",
            answer="暂时无法确认官方进度。",
            executions=[],
            conversation_context=[{"role": "assistant", "media_facts": previous}],
        )

        self.assertEqual(facts["identity"]["title"], "牧神记")
        self.assertNotIn("season_counts", facts)

    def test_identity_matching_prefers_authoritative_ids_over_title_spelling(self):
        facts = {
            "version": 1,
            "identity": {"title": "沧元图", "tmdb_id": "123"},
        }

        self.assertTrue(media_facts_match_context(
            facts,
            {"title": "The Demon Hunter", "tmdb_id": "123"},
        ))
        self.assertFalse(media_facts_match_context(
            facts,
            {"title": "沧元图", "tmdb_id": "456"},
        ))

    def test_validator_rejects_credential_bearing_identity(self):
        self.assertEqual(validate_media_facts({
            "version": 1,
            "identity": {"title": "token=secret-value"},
        }), {})


if __name__ == "__main__":
    unittest.main()
