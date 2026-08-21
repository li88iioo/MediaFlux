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

    def test_base_url_discovery_is_explicit_and_requires_full_refresh(self):
        self.assertIn('id="detectStrmBaseUrlBtn"', self.template)
        self.assertIn('id="strmBaseUrlCandidates"', self.template)
        self.assertIn('id="strmBaseUrlRefresh"', self.template)
        self.assertIn('id="refreshStrmBaseUrlBtn"', self.template)
        self.assertIn("/api/strm/base-url-candidates", self.template)
        self.assertIn("发现候选地址", self.template)
        self.assertIn("不验证 Jellyfin / Emby 是否能够访问", self.template)
        self.assertIn("return strmBaseUrlInput.value.trim()?Promise.resolve():loadStrmBaseUrlCandidates()", self.template)
        self.assertIn("strmBaseUrlInput.value=strmBaseUrlCandidates.value", self.template)
        self.assertIn("baseUrlRefreshPending=true", self.template)
        self.assertIn("完整刷新", self.template)
        self.assertIn("grid-template-columns: max-content minmax(0, 1fr)", self.css)
        self.assertIn(".strm-base-url-refresh", self.css)
        self.assertIn("min-height: 58px", self.css)
        self.assertIn("strmBaseUrlRefresh.hidden=state==='idle'", self.template)
        self.assertNotIn('id="strmBaseUrlRefresh" data-state="idle" style="display: none;"', self.template)

    def test_local_root_picker_uses_the_existing_directory_api_and_selected_id(self):
        self.assertIn("/api/local-media/directories?", self.template)
        self.assertNotIn("/api/local-media/browse?", self.template)
        self.assertIn("data.directories || []", self.template)
        self.assertIn("const selectedPath = String(item?.id || '').trim()", self.template)

    def test_source_list_reserves_previous_height_across_refresh(self):
        self.assertIn("mediaflux:strm-source-rows", self.template)
        self.assertIn("--strm-source-reserved-height", self.template)
        self.assertIn("syncSourceReservation(list)", self.template)
        self.assertIn("mediaflux:strm-source-height", self.template)
        self.assertIn("aria-busy=\"true\"", self.template)
        self.assertIn("min-height: var(--strm-source-reserved-height, 46px)", self.css)
        self.assertIn("overflow-anchor: none", self.css)

    def test_frontend_exposes_only_full_sync_and_keeps_backend_fast_mode_hidden(self):
        self.assertNotIn('id="runStrmNowBtn"', self.template)
        self.assertIn('id="runStrmFullBtn"', self.template)
        self.assertNotIn("/api/strm/run/fast", self.template)
        self.assertIn("/api/strm/run/full", self.template)
        self.assertNotIn("快速同步", self.template)
        self.assertIn("完整校准", self.template)
        self.assertIn('id="strmLastRunMetrics"', self.template)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", self.css)
        self.assertIn(".strm-last-run-metrics", self.css)
        self.assertIn("min-height: 18px", self.css)

    def test_config_loading_and_environment_management_lock_actions(self):
        self.assertIn('id="saveStrmBtn" disabled aria-disabled="true"', self.template)
        self.assertIn("strmConfigReady=true", self.template)
        self.assertIn("saveStrmBtn.disabled=false", self.template)
        self.assertIn("}).catch(error=>{", self.template)
        self.assertIn("isBaseUrlManaged()", self.template)
        self.assertIn("strmBaseUrlCandidates.disabled=!strmConfigReady||managed||!hasCandidates", self.template)
        self.assertIn("播放地址由部署环境管理", self.template)

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
