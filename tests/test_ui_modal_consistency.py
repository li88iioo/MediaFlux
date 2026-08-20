from __future__ import annotations

import unittest
from pathlib import Path


class UiModalConsistencyTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.main_css = (root / "app/static/css/main.css").read_text(encoding="utf-8")
        self.local_css = (root / "app/static/css/local-media.css").read_text(encoding="utf-8")
        self.dashboard_css = (root / "app/static/css/dashboard-workbench.css").read_text(encoding="utf-8")
        self.rss = (root / "app/templates/rss.html").read_text(encoding="utf-8")
        self.local_media = (root / "app/templates/local_media.html").read_text(encoding="utf-8")
        self.proxy = (root / "app/templates/media_proxy.html").read_text(encoding="utf-8")

    def test_subscription_source_and_media_forms_share_one_modal_contract(self):
        self.assertIn('class="rss-sub-modal subscription-modal subscription-source-modal"', self.rss)
        self.assertIn("subscription-source-form", self.rss)
        self.assertIn("RSS SOURCE", self.rss)
        self.assertNotIn('style="height:34px', self.rss)
        self.assertIn(".rss-btn.is-primary", self.main_css)
        self.assertIn(".rss-modal-footer-btns .rss-btn", self.main_css)

    def test_configuration_forms_share_modal_radius_controls_and_actions(self):
        for token in (
            "--modal-radius: 18px;",
            "--modal-control-radius: 10px;",
            "--modal-action-radius: 11px;",
            "--modal-action-height: 42px;",
        ):
            self.assertIn(token, self.main_css)
        self.assertIn("border-radius: var(--modal-radius);", self.main_css)
        self.assertIn(".media-config-dialog .form-input", self.main_css)
        self.assertIn(".lm-source-dialog .form-input", self.local_css)
        self.assertIn(".dashboard-page .media-config-actions .jump-btn", self.dashboard_css)
        self.assertIn("MEDIA SOURCE", self.local_media)
        self.assertIn("PROXY INSTANCE", self.proxy)

    def test_global_confirmation_and_message_dialogs_use_the_same_action_details(self):
        self.assertIn(".app-confirm-dialog,\n.app-message-dialog {", self.main_css)
        self.assertIn("border-radius: var(--modal-radius);", self.main_css)
        self.assertIn("min-height: var(--modal-action-height);", self.main_css)
        self.assertIn("border-radius: var(--modal-action-radius);", self.main_css)


if __name__ == "__main__":
    unittest.main()
