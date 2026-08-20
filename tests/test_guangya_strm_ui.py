from __future__ import annotations

import unittest
from pathlib import Path


class GuangyaStrmUiTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.template = ((root / "app/templates/guangya_strm.html").read_text(encoding="utf-8") + (root / "app/static/js/guangya-strm.js").read_text(encoding="utf-8"))
        self.css = (root / "app/static/css/main.css").read_text(encoding="utf-8")

    def test_tabs_restore_fragment_before_paint_and_keep_accessible_state(self):
        self.assertIn("document.documentElement.dataset.strmInitialTab", self.template)
        self.assertIn("window.history.replaceState", self.template)
        self.assertIn("activateStrmTab(window.location?.hash?.slice(1)||''", self.template)
        for tab in ("config", "schedule", "diagnostics"):
            self.assertIn(f'id="strmTabButton_{tab}"', self.template)
            self.assertIn(f'aria-controls="strmTab_{tab}"', self.template)
            self.assertIn(f'aria-labelledby="strmTabButton_{tab}"', self.template)
        self.assertIn('html[data-strm-initial-tab="schedule"]', self.css)
        self.assertIn('html[data-strm-initial-tab="diagnostics"]', self.css)

    def test_base_url_detection_uses_stable_controls_and_never_silent_overwrites(self):
        self.assertIn('id="detectStrmBaseUrlBtn"', self.template)
        self.assertIn('id="strmBaseUrlCandidates"', self.template)
        self.assertIn("/api/strm/base-url-candidates", self.template)
        self.assertIn("不会自动覆盖当前配置", self.template)
        self.assertIn("return strmBaseUrlInput.value.trim()?Promise.resolve():loadStrmBaseUrlCandidates()", self.template)
        self.assertIn("strmBaseUrlInput.value=strmBaseUrlCandidates.value", self.template)
        self.assertIn("grid-template-columns: max-content minmax(0, 1fr)", self.css)
        self.assertIn("min-height: 42px", self.css)

    def test_source_list_reserves_previous_height_across_refresh(self):
        self.assertIn("mediaflux:strm-source-rows", self.template)
        self.assertIn("--strm-source-reserved-height", self.template)
        self.assertIn("syncSourceReservation(sources.length)", self.template)
        self.assertIn("aria-busy=\"true\"", self.template)
        self.assertIn("min-height: var(--strm-source-reserved-height, 52px)", self.css)
        self.assertIn("overflow-anchor: none", self.css)

    def test_fast_and_full_sync_actions_are_distinct_and_layout_stable(self):
        self.assertIn('id="runStrmNowBtn"', self.template)
        self.assertIn('id="runStrmFullBtn"', self.template)
        self.assertIn("/api/strm/run/fast", self.template)
        self.assertIn("/api/strm/run/full", self.template)
        self.assertIn("快速同步", self.template)
        self.assertIn("完整校准", self.template)
        self.assertIn('id="strmLastRunMetrics"', self.template)
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", self.css)
        self.assertIn(".strm-last-run-metrics", self.css)
        self.assertIn("min-height: 18px", self.css)

    def test_metadata_queue_status_is_explicit_and_keeps_polling_until_drained(self):
        self.assertIn("不阻塞 STRM", self.template)
        self.assertIn("const metadataPending=Math.max(0,Number(metadataQueue.pending||0))", self.template)
        self.assertIn("STRM 已完成 · 元数据后台处理中", self.template)
        self.assertIn("伴随元数据同步已关闭 · 队列暂停", self.template)
        self.assertIn("`队列 ${metadataPending} 项", self.template)
        self.assertIn("metadataQueue.enabled===false", self.template)
        self.assertIn("const shouldPoll=!!s.running||(metadataPending>0&&metadataQueue.enabled!==false)", self.template)


if __name__ == "__main__":
    unittest.main()
