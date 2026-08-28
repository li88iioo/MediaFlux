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

    def test_media_hygiene_recalls_nsfw_filename_cleanup(self):
        names = self._names("把 (xxx.com)-番号.mp4 这种垃圾标题清理掉并刷新 STRM")

        self.assertIn("guangya.media_hygiene.preview", names)

    def test_organize_cleanup_recalls_empty_and_image_only_residuals(self):
        names = self._names("清理光鸭整理来源与执行空间的空媒体目录，剩下 a/xxx.png")

        self.assertIn("guangya.organize.cleanup.preview", names)

    def test_cleanup_keep_followup_recalls_item_review_tool(self):
        names = self._names("刚才的残留计划保留第 2 个，其他判断不变")

        self.assertIn("guangya.organize.cleanup.classify", names)

    def test_ambiguous_media_garbage_request_exposes_both_safe_previews(self):
        names = self._names("帮我整理一下 a 目录媒体目录和媒体信息的垃圾信息")

        self.assertIn("guangya.media_hygiene.preview", names)
        self.assertIn("guangya.organize.cleanup.preview", names)

    def test_guangya_directory_analysis_recalls_observation_and_declarative_plan(self):
        names = self._names("看看光鸭 a 目录里面有哪些文件，分析文件名垃圾前缀")

        self.assertIn("guangya.directory.inspect", names)
        self.assertIn("guangya.change_plan.preview", names)

    def test_followup_object_reference_recalls_declarative_plan(self):
        names = self._names("根据刚才看到的对象引用生成精确改名计划")

        self.assertIn("guangya.change_plan.preview", names)

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
