from __future__ import annotations

import unittest

from app.sensitive_data import (
    contains_sensitive_credential,
    is_sensitive_key,
    redact_sensitive_text,
)


SESSION_BATCH_HELP = (
    "Help on function batch in module mf.session:\n\n"
    "batch(steps: list[dict]) -> dict\n"
    "    将steps中的原生写method/arguments冻结为一张确认卡；参数与单方法相同。全部复核后串行执行，失败/unknown停止后项，不回滚、不重放。仅支持已声明可组合方法；后台方法只能是唯一第一项，后项须独立同步写，不能依赖前项的新结果。\n\n"
)


class SensitiveDataTests(unittest.TestCase):
    def test_python_help_preserves_complete_module_heading_and_signature(self):
        self.assertEqual(len(SESSION_BATCH_HELP), 208)
        for module in ("mf.session", "package.api_key", "package.password"):
            value = SESSION_BATCH_HELP.replace("mf.session", module)
            with self.subTest(module=module):
                self.assertFalse(contains_sensitive_credential(value))
                self.assertEqual(redact_sensitive_text(value), value)

    def test_python_help_still_redacts_credentials_in_the_body(self):
        value = SESSION_BATCH_HELP + "    session:\n\n    private-session\n"
        self.assertTrue(contains_sensitive_credential(value))
        redacted = redact_sensitive_text(value)
        self.assertTrue(redacted.startswith(SESSION_BATCH_HELP))
        self.assertNotIn("private-session", redacted)
        self.assertIn("********", redacted)
        self.assertFalse(contains_sensitive_credential(redacted))

    def test_ascii_credential_keys_adjacent_to_chinese_are_not_hidden(self):
        from app.agent.public_safety import sanitize_public_text
        for prefix in ("凭据", "提示：", "状态描述"):
            value = prefix + "access_token=AbCd1234testCredential9876。"
            with self.subTest(prefix=prefix):
                self.assertTrue(contains_sensitive_credential(value))
                self.assertNotIn("AbCd1234", redact_sensitive_text(value))
                self.assertEqual(sanitize_public_text(value, preserve_format=True), "")
        self.assertFalse(contains_sensitive_credential("媒体字段title=合法片名"))

    def test_detects_environment_style_and_transport_credentials(self):
        cases = (
            "TG_BOT_TOKEN=123456:abcdefghijklmnopqrstuvwxyzABCDE",
            "WEB_SECRET_KEY=not-for-history",
            "JELLYFIN_API_KEY=server-secret",
            "Authorization: Bearer bearer-secret",
            "Authorization: Bearer abc",
            "Authorization: Basic Zm9vOmJhcg==",
            "Authorization: Token custom-secret",
            'Proxy-Authorization: Digest username="alice", response="digest-secret"',
            "https://alice:private-pass@example.invalid/path",
            "Cookie: sid=private-session; theme=dark",
            "session=private-session",
            "session:\n\nprivate-session",
            "api_key:\n    private-value",
            "api_key:\n\n    private-value",
            "api_key\n=\nprivate-value",
            'headers={"api_key":\n"private-value"}',
            "config.password=private-value",
            "config.password:\n\nprivate-value",
            "Help on function batch in module package.password:\n\nprivate-value",
            "Help on function read in module package.password:\n\nbatch(private-value)",
            "Help on function batch in module package.password:\n\nBATCH(private-value)",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature_value",
            "tvly-abcdefghijklmnopqrstuvwxyz123456",
            "hf_abcdefghijklmnopqrstuvwxyz123456",
            "glpat-abcdefghijklmnopqrstuvwxyz123456",
        )
        for value in cases:
            with self.subTest(value=value):
                self.assertTrue(contains_sensitive_credential(value))
                redacted = redact_sensitive_text(value)
                self.assertIn("********", redacted)
                self.assertFalse(contains_sensitive_credential(redacted))

    def test_detects_and_redacts_chinese_credential_assignment(self):
        cases = (
            ("凭据是 hunter2", "hunter2"),
            ("凭证：秘密值", "秘密值"),
            ("授权：秘密值", "秘密值"),
            ("authorization:Bearer 秘密值", "秘密值"),
        )
        for value, secret in cases:
            with self.subTest(value=value):
                self.assertTrue(contains_sensitive_credential(value))
                redacted = redact_sensitive_text(value)
                self.assertIn("********", redacted)
                self.assertNotIn(secret, redacted)
                self.assertFalse(contains_sensitive_credential(redacted))

    def test_keeps_placeholders_and_documentation_examples(self):
        cases = (
            "TG_BOT_TOKEN=${TG_BOT_TOKEN}",
            "WEB_SECRET_KEY=<WEB_SECRET_KEY>",
            "api_key=example",
            "Authorization: Bearer <TOKEN>",
            "password=your_password",
        )
        for value in cases:
            with self.subTest(value=value):
                self.assertFalse(contains_sensitive_credential(value))
                self.assertEqual(redact_sensitive_text(value), value)

    def test_redacts_sensitive_query_inside_named_url_assignment(self):
        secret_signature = "task12-secret-signature"
        secret_token = "task12-secret-token"
        value = (
            "provider failed url=https://download.invalid/media/file.mkv"
            f"?signature={secret_signature}&token={secret_token}&safe=visible"
        )

        self.assertTrue(contains_sensitive_credential(value))
        redacted = redact_sensitive_text(value)
        self.assertNotIn(secret_signature, redacted)
        self.assertNotIn(secret_token, redacted)
        self.assertIn("signature=********", redacted)
        self.assertIn("token=********", redacted)
        self.assertIn("safe=visible", redacted)
        self.assertFalse(contains_sensitive_credential(redacted))

    def test_preserves_authorization_scheme_while_masking_credential(self):
        self.assertEqual(
            redact_sensitive_text("Authorization: Bearer bearer-secret"),
            "Authorization: Bearer ********",
        )
        self.assertEqual(
            redact_sensitive_text("Authorization: Bearer abc"),
            "Authorization: Bearer ********",
        )
        self.assertEqual(
            redact_sensitive_text("Authorization: Basic Zm9vOmJhcg=="),
            "Authorization: Basic ********",
        )
        self.assertEqual(
            redact_sensitive_text("Authorization: Token custom-secret"),
            "Authorization: ********",
        )
        self.assertEqual(
            redact_sensitive_text(
                'Proxy-Authorization: Digest username="alice", response="digest-secret"'
            ),
            "Proxy-Authorization: ********",
        )

    def test_redacts_mapping_query_cookie_and_userinfo_without_touching_safe_fields(self):
        value = (
            "headers={'X-Emby-Token': 'header-secret'} "
            "payload={'api_key':'query-secret', 'name': 'visible'} "
            "GET https://alice:pass@example.invalid/x?passkey=tracker-secret&safe=visible "
            "Cookie: sid=session-secret"
        )
        redacted = redact_sensitive_text(value)
        for secret in (
            "header-secret",
            "query-secret",
            "alice:pass",
            "tracker-secret",
            "session-secret",
        ):
            self.assertNotIn(secret, redacted)
        self.assertIn("name': 'visible", redacted)
        self.assertIn("safe=visible", redacted)

    def test_redacts_percent_encoded_provider_tokens_and_jwt(self):
        cases = (
            "callback/sk%2Dabcdefghijklmnopqrstuv",
            "eyJhbGciOiJIUzI1NiJ9%2EeyJzdWIiOiIxMjM0NTY3ODkwIn0%2Esignature_value",
        )
        for value in cases:
            with self.subTest(value=value):
                self.assertTrue(contains_sensitive_credential(value))
                redacted = redact_sensitive_text(value)
                self.assertIn("********", redacted)
                self.assertFalse(contains_sensitive_credential(redacted))

    def test_shared_sensitive_key_classifier_covers_support_bundle_secrets(self):
        for key in (
            "DOUBAN_DBCL2",
            "TRACKER_PASSKEY",
            "SERVICE_AUTHKEY",
            "CALLBACK_SIGNATURE",
            "SESSION_ID",
            "JELLYFIN_SESSION_ID",
            "LEGACY_SESSIONID",
            "CREDENTIALS",
        ):
            with self.subTest(key=key):
                self.assertTrue(is_sensitive_key(key))
        self.assertFalse(is_sensitive_key("NORMAL_VALUE"))


if __name__ == "__main__":
    unittest.main()
