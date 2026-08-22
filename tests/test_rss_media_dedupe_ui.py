from pathlib import Path
import unittest


class RSSMediaDedupeUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = Path("app/templates/rss.html").read_text(encoding="utf-8")
        cls.css = Path("app/static/css/main.css").read_text(encoding="utf-8")

    def test_exact_library_filter_is_removed_from_default_form_without_removing_backend_notices(self) -> None:
        self.assertNotIn('id="f_skip_existing"', self.template)
        self.assertNotIn('id="f_media_tmdb_id"', self.template)
        self.assertNotIn('id="f_media_season"', self.template)
        self.assertNotIn("media_tmdb_id: skipExisting", self.template)
        self.assertNotIn("skip_existing_episodes: skipExisting", self.template)
        self.assertNotIn(".rss-library-dedupe", self.css)
        self.assertIn("media_binding_bypassed", self.template)
        self.assertIn("无法确认所有条目属于同一剧目", self.template)
        self.assertIn("e.skip_reason", self.template)

    def test_rss_source_cards_expose_stable_direct_pause_and_enable_actions(self) -> None:
        self.assertIn('class="rss-btn rss-sub-toggle ${en?', self.template)
        self.assertIn('onclick="toggleSub(${s.id},${en},this)"', self.template)
        self.assertIn("async function toggleSub(id,enabled,trigger)", self.template)
        self.assertIn("JSON.stringify({enabled:nextEnabled})", self.template)
        self.assertIn("await loadSubs({preserve:true});", self.template)
        self.assertIn("nextEnabled?'启用中':'暂停中'", self.template)
        self.assertIn('.rss-sub-toggle {', self.css)
        self.assertIn('min-width: 72px;', self.css)
        self.assertIn('.subscription-section-toolbar .subscription-rss-create { width: 38px;', self.css)
        self.assertIn('.rss-sub-actions .rss-btn:not(.rss-sub-toggle) span { display: none; }', self.css)

    def test_rss_panel_reflows_without_mobile_horizontal_overflow(self) -> None:
        self.assertIn('.subscription-rss-stats { grid-template-columns: repeat(2, minmax(0, 1fr));', self.css)
        self.assertIn('.rss-sub-actions { width: 100%; justify-content: flex-end; flex-wrap: wrap; }', self.css)
        self.assertIn('.rss-sub-url-inline { width: 100%; flex-basis: 100%; }', self.css)
        self.assertIn('.rss-entry-head { grid-template-columns: minmax(0, 1fr);', self.css)
        self.assertIn('.rss-batch-actions { display: grid; grid-template-columns: minmax(0, 1fr);', self.css)
        self.assertIn('.rss-entry-card { grid-template-columns: auto 32px minmax(0, 1fr);', self.css)
        self.assertIn('.rss-entry-actions { grid-column: 2 / -1; width: 100%;', self.css)

    def test_subscription_modals_follow_mobile_visual_viewport(self) -> None:
        overlay = self.css.split('.rss-sub-modal {', 1)[1].split('}', 1)[0]
        self.assertIn('overflow: hidden;', overlay)
        self.assertNotIn('overflow-y: auto;', overlay)
        self.assertIn('max-height: min(820px, calc(100dvh - 28px - env(safe-area-inset-top, 0px) - env(safe-area-inset-bottom, 0px)));', self.css)
        self.assertIn('.subscription-modal { align-items: flex-start; padding: 8px 8px calc(8px + env(safe-area-inset-bottom)); }', self.css)
        self.assertIn('max-height: calc(100dvh - 16px - env(safe-area-inset-top, 0px) - env(safe-area-inset-bottom, 0px));', self.css)
        self.assertNotIn('.subscription-modal-card { max-height: calc(100vh - 16px); }', self.css)

    def test_rss_lists_keep_cards_static_and_animate_numbers_only(self) -> None:
        self.assertIn("function lockRssListHeight(list)", self.template)
        self.assertIn("requestAnimationFrame(()=>requestAnimationFrame", self.template)
        self.assertIn("window.MFAnim.countUp", self.template)
        self.assertNotIn("window.MFAnim.staggerIn", self.template)
        self.assertNotIn("window.MFAnim.crossfade", self.template)
        self.assertIn("preserve&&hadContent?lockRssListHeight(list)", self.template)
        self.assertIn("loadSubs({preserve:true})", self.template)
