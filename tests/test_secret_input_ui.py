"""全局密码与 API Key 输入框显隐控件契约。"""
from __future__ import annotations

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SCRIPT = ROOT / "app" / "static" / "js" / "app.js"
STYLES = ROOT / "app" / "static" / "css" / "main.css"
TEMPLATES = ROOT / "app" / "templates"


class _FormOwnershipParser(HTMLParser):
    """验证浏览器关心的 form 归属，不尝试完整解析 Jinja。"""

    def __init__(self):
        super().__init__()
        self.forms: list[tuple[int, int, str]] = []
        self.nested_forms: list[tuple[int, int]] = []
        self.orphan_closes: list[tuple[int, int]] = []
        self.passwords_without_form: list[tuple[int, int, str]] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form":
            if self.forms:
                self.nested_forms.append(self.getpos())
            self.forms.append((*self.getpos(), attributes.get("id", "")))
        elif tag == "input" and attributes.get("type", "").lower() == "password":
            if not self.forms:
                self.passwords_without_form.append((*self.getpos(), attributes.get("id", "")))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag != "form":
            return
        if self.forms:
            self.forms.pop()
        else:
            self.orphan_closes.append(self.getpos())


class SecretInputUIContractTests(unittest.TestCase):
    def setUp(self):
        self.script = APP_SCRIPT.read_text(encoding="utf-8")
        self.styles = STYLES.read_text(encoding="utf-8")

    def test_global_script_enhances_static_and_dynamic_password_inputs(self):
        for contract in (
            "function enhanceSecretInput",
            "function enhanceSecretInputs",
            "input[type=\"password\"]",
            "data-secret-toggle",
            "secret-input-shell",
            "secret-input-toggle",
            "data-secret-enhanced",
            "aria-pressed",
            "eye-off",
            "enhanceSecretInputs(node)",
        ):
            self.assertIn(contract, self.script)
        self.assertRegex(
            self.script,
            re.compile(
                r"input\.type\s*=\s*visible\s*\?\s*'text'\s*:\s*'password'",
                re.S,
            ),
        )
        self.assertIn("button.type = 'button'", self.script)

    def test_secret_control_reserves_space_and_overlays_stable_button(self):
        self.assertRegex(
            self.styles,
            re.compile(
                r'input\[type="password"\]\.form-input\s*\{[^}]*padding-right:\s*44px',
                re.S,
            ),
        )
        self.assertRegex(
            self.styles,
            re.compile(
                r"\.secret-input-shell\s*\{[^}]*position:\s*relative[^}]*width:\s*100%",
                re.S,
            ),
        )
        self.assertRegex(
            self.styles,
            re.compile(
                r"\.secret-input-toggle\s*\{[^}]*position:\s*absolute[^}]*right:[^;}]+"
                r"[^}]*width:\s*32px[^}]*height:\s*32px",
                re.S,
            ),
        )
        self.assertIn('.secret-input-toggle:focus-visible', self.styles)

    def test_all_current_password_surfaces_load_shared_enhancement(self):
        expected = {
            "login.html": 1,
            "settings.html": 8,
            "dashboard.html": 2,
            "organize.html": 1,
            "media_proxy.html": 1,
        }
        for filename, minimum in expected.items():
            html = (TEMPLATES / filename).read_text(encoding="utf-8")
            with self.subTest(filename=filename):
                self.assertGreaterEqual(html.count('type="password"'), minimum)
        login = (TEMPLATES / "login.html").read_text(encoding="utf-8")
        base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
        self.assertIn("js/app.js", login)
        self.assertIn("js/app.js", base)


    def test_password_inputs_have_form_owners_without_nested_forms(self):
        for filename in (
            "login.html",
            "settings.html",
            "dashboard.html",
            "organize.html",
            "media_proxy.html",
        ):
            parser = _FormOwnershipParser()
            parser.feed((TEMPLATES / filename).read_text(encoding="utf-8"))
            with self.subTest(filename=filename):
                self.assertEqual(parser.nested_forms, [])
                self.assertEqual(parser.orphan_closes, [])
                self.assertEqual(parser.passwords_without_form, [])
                self.assertEqual(parser.forms, [])

    def test_settings_use_one_form_per_save_action(self):
        settings = (TEMPLATES / "settings.html").read_text(encoding="utf-8")
        self.assertIn('<div class="card card-pad" id="settingsForm">', settings)
        self.assertNotIn('<form class="card card-pad" id="settingsForm"', settings)
        self.assertEqual(settings.count('<form class="settings-panel'), 7)
        save_buttons = re.findall(
            r'<button type="button" class="btn btn-primary" data-save-settings[^>]*>',
            settings,
        )
        self.assertEqual(len(save_buttons), 7)
        self.assertTrue(all("disabled" in button for button in save_buttons))
        self.assertEqual(settings.count('onsubmit="return false;" novalidate'), 7)

    def test_secret_surfaces_use_action_scoped_forms(self):
        dashboard = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
        organize = (TEMPLATES / "organize.html").read_text(encoding="utf-8")
        proxy = (TEMPLATES / "media_proxy.html").read_text(encoding="utf-8")
        self.assertIn('<form class="media-config-panel" data-tab-panel="jellyfin" onsubmit="return false;" novalidate>', dashboard)
        self.assertIn('<form class="media-config-panel" data-tab-panel="emby" onsubmit="return false;" novalidate hidden>', dashboard)
        self.assertIn('id="organizeConfigForm" onsubmit="return false;" novalidate', organize)
        self.assertIn('id="proxyInstanceForm" role="dialog"', proxy)
        self.assertIn('onsubmit="return false;" novalidate', proxy)

    def test_dashboard_secret_inputs_are_not_interactive_controls_nested_in_labels(self):
        dashboard = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
        self.assertNotIn(
            '<label class="form-group"><span class="form-label">API Key</span><input type="password"',
            dashboard,
        )
        self.assertNotIn(
            '<label class="form-group"><span class="form-label">Emby Token</span><input type="password"',
            dashboard,
        )
        self.assertIn('for="jellyfinApiKey"', dashboard)
        self.assertIn('id="jellyfinApiKey"', dashboard)
        self.assertIn('for="embyToken"', dashboard)
        self.assertIn('id="embyToken"', dashboard)


if __name__ == "__main__":
    unittest.main()
