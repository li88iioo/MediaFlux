"""原生本地诊断：复用公共汇总、完整来源/错误、真实SQLite分页与零业务写入。"""
from __future__ import annotations

from contextlib import contextmanager
import json
from unittest.mock import patch

from app import database as db, sdk
from app.repositories import rss as rss_repo
from app.agent.kernel.bootstrap import build_tool_catalog
from app.modules import diagnostics as diag
from tests.support import IsolatedDatabaseTestCase


def aggregate(**overrides):
    value = {"downloads_active": 0, "downloads_review": 0, "rss_subscriptions": 0, "media_subscriptions": 0,
             "subscriptions_total": 0, "rss_pending": 0, "rss_failed": 0, "organize_issues": 0,
             "strm_failures": 0, "strm_last_status": "", "strm_last_at": "", "issues": 0,
             "issue_source": "none", "healthy": True, "error": ""}
    return {**value, **overrides}


class AutomationPipelineTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            conn.execute("DELETE FROM rss_entries")
            conn.execute("DELETE FROM rss_items")

    def test_native_signatures_and_old_specs_are_retired(self):
        assert {"diagnostics.configuration", "diagnostics.component", "diagnostics.pipeline"} <= sdk.METHODS.keys()
        assert not {"config.diagnose", "config.explain_component", "automation.diagnose_pipeline"} & {spec.name for spec in build_tool_catalog().visible()}
        with self.assertRaises(TypeError):
            sdk.prepare_sdk_method("diagnostics.pipeline", {"token": "secret"}, owner="webk:v1:" + "a" * 64)
        for limit, offset in ((0, 0), (101, 0), (True, 0), (1, -1), (1, False)):
            with self.subTest(limit=limit, offset=offset), self.assertRaises(ValueError):
                diag.pipeline(limit, offset)

    def test_empty_database_is_not_configured_with_complete_sources(self):
        result = diag.pipeline()
        assert result["ok"] and result["status"] == "not_configured"
        assert result["data"]["network_accessed"] is False
        assert result["data"]["errors"] == []
        assert set(result["data"]["sources"]) == {"automation_database", "rss"}
        local = result["data"]["sources"]["automation_database"]
        assert {"media_subscriptions", "subscriptions_total", "strm_last_at", "healthy"} <= local.keys()
        assert result["data"]["sources"]["rss"]["status"] == "not_configured"
        assert {row["source"] for row in result["evidence"]} == {"automation_database", "rss"}

    def test_status_retains_active_counts_and_last_failed_run_without_open_failure(self):
        for value, status in ((aggregate(downloads_active=3, subscriptions_total=2, rss_pending=7, strm_last_status="completed"), "healthy"),
                (aggregate(strm_last_status="failed"), "attention"), (aggregate(strm_last_status=" FAILED "), "attention"), (aggregate(downloads_review=2, issues=2), "attention")):
            with self.subTest(status=status), patch("app.services.build_automation_summary", return_value=value):
                result = diag.pipeline()
            assert result["status"] == status
            assert result["data"]["sources"]["automation_database"] == value

    def test_partial_failure_preserves_other_source_and_all_unavailable_is_explicit(self):
        with patch("app.services.build_automation_summary", side_effect=RuntimeError("SECRET /srv/private")):
            result = diag.pipeline()
        assert result["ok"] and result["status"] == "partial"
        assert result["data"]["sources"]["automation_database"] is None
        assert result["data"]["sources"]["rss"]["ok"]
        assert result["data"]["errors"][0]["exception_type"] == "RuntimeError"
        assert "SECRET" not in json.dumps(result, ensure_ascii=False)
        assert "/srv/private" not in json.dumps(result, ensure_ascii=False)
        with patch("app.services.build_automation_summary", return_value=aggregate(error="自动化状态暂不可用")), patch(
                "app.modules.rss_subscriptions.diagnose", return_value={"ok": False, "status": "unavailable", "error": "RSS诊断不可用", "data": {}}):
            result = diag.pipeline()
        assert not result["ok"] and result["status"] == "unavailable"
        assert {row["source"] for row in result["data"]["errors"]} == {"automation_database", "rss"}
        assert result["data"]["sources"]["rss"]["error"] == "RSS诊断不可用"

    def test_untrusted_extra_fields_and_invalid_status_do_not_leak_credentials_or_paths(self):
        value = aggregate(strm_last_status="token=TOKEN_9f /srv/private", strm_last_at="private-host",
                          token="HASH_SECRET", path="/srv/private", url="http://192.0.2.10", guid="GUID_SECRET", title="TITLE_SECRET")
        with patch("app.services.build_automation_summary", return_value=value):
            result = diag.pipeline()
        assert result["status"] == "partial"
        assert result["data"]["sources"]["automation_database"]["strm_last_status"] == "unknown"
        assert result["data"]["sources"]["automation_database"]["strm_last_at"] is None
        public = json.dumps(result, ensure_ascii=False)
        for secret in ("TOKEN_9f", "/srv/private", "private-host", "HASH_SECRET", "GUID_SECRET", "TITLE_SECRET", "192.0.2.10"):
            assert secret not in public

    def test_real_readonly_sqlite_rss_pages_are_exhaustive_and_match_public_service(self):
        ids = []
        for index in range(3):
            identifier = rss_repo.add_rss_subscription(name=f"source-{index}", urls="https://rss.invalid/?passkey=SECRET", enabled=1)
            row = rss_repo.add_rss_entry_with_media(identifier, f"entry-{index}", f"guid-{index}")
            assert row is not None
            with db.get_conn() as conn:
                conn.execute("UPDATE rss_entries SET status='failed' WHERE id=?", (row["id"],))
            ids.append(identifier)
        original = db.get_conn
        writes = []
        @contextmanager
        def readonly():
            with original() as conn:
                conn.execute("PRAGMA query_only=ON")
                conn.set_trace_callback(lambda sql: writes.append(sql) if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")) else None)
                yield conn
        observed = []
        offset = 0
        with patch.object(db, "get_conn", readonly):
            while True:
                result = diag.pipeline(limit=1, offset=offset)
                rss = result["data"]["sources"]["rss"]
                attention = rss["data"]["attention"]
                observed.extend(row["subscription_id"] for row in attention["subscriptions"])
                assert rss["data"]["subscriptions"]["total"] == len(ids)
                if not attention["has_more"]:
                    break
                offset = attention["next"]["offset"]
                assert offset <= len(ids)
        assert set(observed) == set(ids) and len(observed) == len(ids)
        assert not writes
        assert "SECRET" not in json.dumps(result, ensure_ascii=False)
