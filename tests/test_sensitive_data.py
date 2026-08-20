from __future__ import annotations

import unittest

from app.sensitive_data import (
    contains_sensitive_credential,
    is_sensitive_key,
    redact_sensitive_text,
)


class SensitiveDataTests(unittest.TestCase):
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
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature_value",
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
