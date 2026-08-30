from __future__ import annotations

import unittest
from pathlib import Path


class TabLazyLoadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.local_media = Path("app/static/js/local-media.js").read_text(encoding="utf-8")
        cls.subscriptions = Path("app/static/js/subscriptions.js").read_text(encoding="utf-8")
        cls.rss = Path("app/templates/rss.html").read_text(encoding="utf-8")
        cls.downloads = (Path("app/templates/downloads.html").read_text(encoding="utf-8") + Path("app/static/js/downloads.js").read_text(encoding="utf-8"))
        cls.strm = (Path("app/templates/guangya_strm.html").read_text(encoding="utf-8") + Path("app/static/js/guangya-strm.js").read_text(encoding="utf-8"))
        cls.proxy = Path("app/templates/media_proxy.html").read_text(encoding="utf-8")

    def test_local_media_items_wait_for_manual_tab(self) -> None:
        self.assertIn(
            "function loadAll(manual = false, {includeItems = false} = {})",
            self.local_media,
        )
        self.assertIn(
            "loadAll(false, {includeItems: currentTab === 'manual'})",
            self.local_media,
        )
        self.assertIn(
            "if (currentTab === 'manual' && (hasLoadedLocalMedia || refreshing))",
            self.local_media,
        )
        self.assertIn("loadAll(false, {includeItems: true})", self.local_media)
        poll_block = self.local_media[
            self.local_media.index("function schedulePoll()"):
            self.local_media.index("function sourceCard(")
        ]
        self.assertIn("await loadAll(false);", poll_block)
        self.assertNotIn("includeItems", poll_block)
        self.assertNotIn(
            "const shouldLoadItems = currentTab === 'manual' || currentManual",
            self.local_media,
        )
        self.assertNotIn("!hasLoadedLocalMedia || currentTab === 'manual'", self.local_media)

    def test_subscription_panels_load_once_even_when_empty(self) -> None:
        self.assertIn("loaded: {media: false, watchlist: false, runs: false}", self.subscriptions)
        self.assertIn("if (normalized === 'rss') return window.ensureRssPanelLoaded?.()", self.subscriptions)
        self.assertIn("rssSubsLoaded&&rssEntriesLoaded", self.rss)
        self.assertIn("rssPanelLoadPromise", self.rss)
        self.assertNotIn("\nloadSubs();\nloadEntries();\n", self.rss)

    def test_download_logs_and_issues_wait_for_their_tabs(self) -> None:
        self.assertIn("if(isIssues&&!hasLoadedDownloadIssues){", self.downloads)
        self.assertIn("loadIssues(downloadIssuePage).then", self.downloads)
        self.assertIn("if(isLogs&&!hasLoadedDownloadLogs){", self.downloads)
        self.assertIn("loadLogs(downloadLogPage).then", self.downloads)
        self.assertNotIn("syncOverviewPolling();loadLogs(1)", self.downloads)
        self.assertNotIn("if(normalizedDownloadView!=='issues')loadIssues(1)", self.downloads)

    def test_strm_secondary_panels_load_after_config_and_activation(self) -> None:
        self.assertIn("if(strmConfigReady){", self.strm)
        self.assertIn("void ensureStrmTabLoaded(normalized)", self.strm)
        self.assertIn("if(normalized==='schedule'&&alreadyLoaded)void loadStatus()", self.strm)
        self.assertIn("if(tab==='schedule')return Promise.all([validateCron(),loadStatus()])", self.strm)
        self.assertIn("if(tab==='diagnostics')return Promise.all([loadIndexDiagnostics(),loadFailures()])", self.strm)
        self.assertIn("void ensureStrmTabLoaded(activeStrmTab)", self.strm)

    def test_media_proxy_records_wait_for_records_tab(self) -> None:
        self.assertIn("async function loadProxyBase(background=false)", self.proxy)
        self.assertIn("if(tab==='records')await ensureProxyRecordsLoaded()", self.proxy)
        self.assertIn("if(activeProxyTab==='records')await loadProxyRecords", self.proxy)
        self.assertNotIn("bindingModal.close()));loadAll();", self.proxy)


if __name__ == "__main__":
    unittest.main()
