from __future__ import annotations

import unittest

from app.agent.capability_retrieval import infer_media_intent
from app.agent.llm_router import _native_read_capabilities
from app.agent.tools import build_tool_registry


class AgentCapabilityRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = build_tool_registry()

    def _names(self, message: str) -> set[str]:
        return {
            self.registry.native_tool_name(item["name"])
            for item in _native_read_capabilities(self.registry, message)
        }

    def test_official_progress_covers_web_library_and_indexer_evidence(self):
        names = self._names("沧元图官方更新到多集啦？")

        self.assertIn("web.search", names)
        self.assertIn("library.check_updates", names)
        self.assertIn("indexer.search_resources", names)

    def test_official_only_scope_excludes_local_and_resource_sources(self):
        profile = infer_media_intent("只查官方：沧元图现在播到第几集，不要本地和资源")
        names = self._names("只查官方：沧元图现在播到第几集，不要本地和资源")

        self.assertEqual(profile.forbidden_sources, ("local_library", "resource_index"))
        self.assertIn("web.search", names)
        self.assertNotIn("library.check_updates", names)
        self.assertNotIn("indexer.search_resources", names)

    def test_resource_search_does_not_require_public_web(self):
        profile = infer_media_intent("搜索沧元图第三季第22集资源")
        names = self._names("搜索沧元图第三季第22集资源")

        self.assertEqual(profile.presentation_hint, "resource_candidates")
        self.assertIn("indexer.search_resources", names)
        self.assertNotIn("web.search", names)

    def test_episode_numbering_recalls_fact_sources_without_resource_takeover(self):
        profile = infer_media_intent("第三季第22集是多少集？")
        names = self._names("第三季第22集是多少集？")

        self.assertIn("episode_numbering", profile.domains)
        self.assertEqual(profile.presentation_hint, "narrative")
        self.assertIn("web.search", names)
        self.assertIn("library.count_series_episodes", names)
        self.assertNotIn("indexer.search_resources", names)


if __name__ == "__main__":
    unittest.main()
