"""只读诊断：白名单表的非凭据列可查，凭据/私有表/写入/越权身份一律拒绝，日志按条件检索并打码。"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from unittest.mock import patch

import pytest

from app import database as db, sdk
from app.agent.kernel.bootstrap import build_tool_catalog
from app.modules import diagnostics as diag
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.state import InMemorySessionStateStore
from tests.support import IsolatedDatabaseTestCase, isolated_test_database
from app.agent.kernel import sandbox
from app.agent.kernel.capabilities import ToolCatalog
from tests.test_agent_code_sandbox import make_context
from types import SimpleNamespace

WEB, TG = "webk:v1:" + "a" * 64, "tg:v1:someone"
SECRET = "passkey=SECRETPASSKEY123456"


def query(sql="", limit=50):
    with sdk.call_owner(WEB):
        return sdk.diagnostics.query_db(sql, limit)


class DiagnosticQueryTests(IsolatedDatabaseTestCase):
    def setUp(self):
        self.continuation = {
            "version": 1, "owner": "tg:v1:another-user", "session_id": "another-session",
            "delivery_id": "another-delivery", "lifecycle_id": "another-lifecycle",
            "html": "另一位用户的私有续行正文", "delivery_route": {"chat_id": "-1001234567890"},
        }
        event_json = json.dumps({"title": "Media Agent", "_agent_continuation_delivery": self.continuation,
                                 "_delivery_route": self.continuation["delivery_route"]}, ensure_ascii=False)
        with db.get_conn() as conn:
            conn.execute("DELETE FROM task_runs")
            conn.executemany(
                "INSERT INTO task_runs(task_name,trigger_type,status,started_at,error) VALUES(?,?,?,?,?)",
                [("strm_sync", "cron", "failed", "2026-11-01", f"拉取 https://t.example/rss?{SECRET} 超时"),
                 ("strm_sync", "cron", "success", "2026-11-02", ""),
                 ("organize", "manual", "failed", "2026-11-02", "目标目录不存在")])
            conn.execute("DELETE FROM telegram_notification_outbox")
            conn.executemany(
                "INSERT INTO telegram_notification_outbox("
                "event_key,topic,chat_id,event_json,status,attempts,last_error,next_attempt_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,'2026-11-03','2026-11-03','2026-11-03')",
                [("private-continuation", "agent", "-1001234567890", event_json, "pending", 0, ""),
                 ("download-result", "download", "", "{}", "retry_wait", 2, "投递超时")])
        db.add_rss_subscription("Mikan", f"https://mikan.example/rss?{SECRET}")

    def test_schema_lists_operational_tables_without_credential_columns(self):
        tables = {row["table"]: row["columns"] for row in query()["data"]["tables"]}
        assert "task_runs" in tables and "rss_items" in tables
        assert "urls" not in tables["rss_items"] and "name" in tables["rss_items"]
        assert "payload" not in tables["rss_entries"] and "title" in tables["rss_entries"]
        assert set(tables["telegram_notification_outbox"]) == {
            "id", "event_key", "thread_key", "topic", "importance", "revision", "delivered_revision",
            "status", "attempts", "lease_generation", "next_attempt_at", "last_error", "sent_at",
            "created_at", "updated_at",
        }
        assert not {"agent_kernel_events", "agent_kernel_refs", "agent_media_preferences", "settings_kv"} & tables.keys()
        for table, columns in tables.items():
            assert not {"api_key", "smb_pass", "owner_digest", "chat_id", "payload_auth"} & set(columns), table

    def test_aggregates_and_redacts_credentials_inside_values(self):
        result = query("SELECT status, count(*) AS n FROM task_runs GROUP BY status ORDER BY status")
        assert result["data"] == {"columns": ["status", "n"], "rows": [["failed", 2], ["success", 1]], "truncated": False}
        errors = query("SELECT error FROM task_runs WHERE status='failed' ORDER BY id")["data"]["rows"]
        assert "SECRETPASSKEY" not in str(errors) and "passkey=" in errors[0][0]

    def test_limit_reports_truncation(self):
        result = query("SELECT id FROM task_runs", limit=2)
        assert len(result["data"]["rows"]) == 2 and result["data"]["truncated"] is True

    def test_credential_columns_private_tables_and_catalog_are_denied(self):
        for sql, hint in (("SELECT * FROM rss_items", "rss_items.urls"),
                          ("SELECT name FROM rss_items WHERE urls LIKE '%x%'", "rss_items.urls"),
                          ("SELECT payload FROM rss_entries", "rss_entries.payload"),
                          ("SELECT json_extract(payload, '$.owner') FROM rss_entries", "rss_entries.payload"),
                          ("SELECT event_json FROM agent_kernel_events", "agent_kernel_events"),
                          ("SELECT value FROM settings_kv", "settings_kv"),
                          ("SELECT name FROM sqlite_master", "sqlite_master")):
            with self.subTest(sql=sql), pytest.raises(ValueError) as caught:
                query(sql)
            assert hint in str(caught.value)

    def test_outbox_pipeline_blocks_private_json_and_preserves_diagnostics(self):
        sql = (
            "SELECT json_extract(event_json, '$._agent_continuation_delivery.html', "
            "'$._agent_continuation_delivery.owner', '$._agent_continuation_delivery.session_id', "
            "'$._agent_continuation_delivery.delivery_route.chat_id') FROM telegram_notification_outbox "
            "WHERE topic='agent'"
        )
        # 证明反例来自真实隔离 outbox，不能因缺少数据或错误 JSON 路径而假通过。
        with db.get_conn() as conn:
            values = json.loads(conn.execute(sql).fetchone()[0])
        assert values == [self.continuation["html"], self.continuation["owner"],
                          self.continuation["session_id"], self.continuation["delivery_route"]["chat_id"]]

        for statement in (
            "SELECT event_json FROM telegram_notification_outbox", sql,
            "SELECT count(*) FROM telegram_notification_outbox WHERE json_extract(event_json, '$._agent_continuation_delivery.owner') IS NOT NULL",
            "WITH delivery AS (SELECT event_json AS payload FROM telegram_notification_outbox) SELECT json_extract(payload, '$._agent_continuation_delivery.html') FROM delivery",
        ):
            with self.subTest(sql=statement), pytest.raises(ValueError) as caught:
                query(statement)
            assert "telegram_notification_outbox.event_json" in str(caught.value)
            assert all(value not in str(caught.value) for value in values)
        tables = {row["table"]: row["columns"] for row in query()["data"]["tables"]}
        assert "event_json" not in tables["telegram_notification_outbox"]
        assert "payload" not in tables["rss_entries"]
        statistics = "SELECT topic,status,count(*) AS n,sum(attempts) AS attempts FROM telegram_notification_outbox GROUP BY topic,status ORDER BY topic,status"
        assert query(statistics)["data"] == {
            "columns": ["topic", "status", "n", "attempts"],
            "rows": [["agent", "pending", 1, 0], ["download", "retry_wait", 1, 2]], "truncated": False,
        }
        assert query("SELECT last_error FROM telegram_notification_outbox WHERE attempts > 0")["data"]["rows"] == [["投递超时"]]

    def test_statements_other_than_one_select_never_run(self):
        for sql in ("DELETE FROM task_runs", "SELECT 1; DELETE FROM task_runs",
                    "WITH x AS (SELECT 1) DELETE FROM task_runs", "PRAGMA table_info(task_runs)"):
            with self.subTest(sql=sql), pytest.raises(ValueError):
                query(sql)
        assert query("SELECT count(*) FROM task_runs")["data"]["rows"] == [[3]]

    def test_long_query_is_interrupted(self):
        with patch.object(diag, "_QUERY_SECONDS", 0.0), pytest.raises(ValueError, match="已中止"):
            query("SELECT count(*) FROM task_runs a, task_runs b, task_runs c, task_runs d, task_runs e, "
                  "task_runs f, task_runs g, task_runs h, task_runs i, task_runs j, task_runs k, task_runs l")

    def test_readonly_connection_cannot_write_even_without_authorizer(self):
        with diag.closing(diag._connect()) as conn, pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM task_runs")


def test_only_web_principals_can_run_native_diagnostics():
    names = {tool.name for tool in build_tool_catalog().visible()}
    assert {"agent.runtime_status", "diagnostics.query_db", "diagnostics.search_logs"}.isdisjoint(names)
    from app import config
    for enabled in (False, True):
        with patch.object(config, "get_bool", return_value=enabled):
            for owner in (WEB, TG):
                with sdk.call_owner(owner):
                    assert sdk.diagnostics.runtime() == {"agent_enabled": enabled, "telegram_enabled": enabled, "model_routing_enabled": enabled}
    for name in ("query_db", "search_logs"):
        with patch.object(diag, name) as service:
            for owner in (TG, "api:other"):
                with sdk.call_owner(owner), pytest.raises(PermissionError):
                    getattr(sdk.diagnostics, name)()
            service.assert_not_called()
            with sdk.call_owner(WEB):
                getattr(sdk.diagnostics, name)()
            service.assert_called_once()


@pytest.fixture
def logs(tmp_path, monkeypatch):
    monkeypatch.setattr(diag, "LOG_DIR", tmp_path)
    (tmp_path / "app.log").write_text("\n".join((
        "2026-11-03 10:00:00.000 | INFO    | app.strm | STRM 同步开始",
        "2026-11-03 10:00:01.000 | ERROR   | app.clients.base | Jellyfin 请求失败 api_key=abcdef1234567890",
        "Traceback (most recent call last):",
        '  File "x.py", line 1, in <module>',
        "2026-11-03 10:00:02.000 | WARNING | app.organize | 光鸭整理跳过 1 项",
        "2026-11-03 10:00:03.000 | ERROR   | app.organize | 光鸭整理失败",
    )), encoding="utf-8")
    (tmp_path / "app.log.2026-11-01").write_text("2026-11-01 08:00:00.000 | ERROR   | app.x | 旧错误", encoding="utf-8")
    return tmp_path


def search(**arguments):
    with sdk.call_owner(WEB):
        return sdk.diagnostics.search_logs(**arguments)


def test_logs_filter_by_level_and_keyword_and_keep_tracebacks(logs):
    errors = search(level="ERROR")
    assert len(errors["data"]["records"]) == 2
    assert "Traceback" in errors["data"]["records"][0] and "abcdef1234567890" not in errors["data"]["records"][0]
    assert [r[-6:] for r in search(query="光鸭")["data"]["records"]] == ["跳过 1 项", "光鸭整理失败"]
    assert search(limit=1)["data"]["records"][0].endswith("光鸭整理失败")
    assert search(date="2026-11-01")["data"]["records"] == ["2026-11-01 08:00:00.000 | ERROR   | app.x | 旧错误"]


def test_logs_reject_bad_arguments_and_report_missing_days(logs):
    for arguments in ({"date": "2026/11/01"}, {"level": "FATAL"}, {"limit": 0}, {"query": "x" * 121}, {"path": "/etc"}):
        with pytest.raises((ValueError, TypeError)):
            search(**arguments)
    missing = search(date="2026-10-01")
    assert missing["ok"] is False and missing["status"] == "not_found"


def test_long_log_records_and_database_cells_are_complete_and_redacted(logs):
    detail = "堆栈" * 1000 + "\nValueError: 真正原因 " + SECRET
    (logs / "app.log").write_text("2026-11-03 10:00:00.000 | ERROR | app.x | " + detail)
    data = search(query="真正原因")["data"]
    assert data["scan_complete"] and not data["truncated"] and data["matched_in_scan"] == 1
    assert "真正原因" in data["records"][0] and "SECRETPASSKEY" not in data["records"][0]
    with isolated_test_database():
        value = query("SELECT '" + detail + "' AS reason")["data"]["rows"][0][0]
        assert len(value) > 500 and "真正原因" in value and "SECRETPASSKEY" not in value


def test_log_tail_does_not_promote_partial_traceback_to_complete_record(logs, monkeypatch):
    prefix = "2026-11-03 10:00:00.000 | ERROR | app.x | " + "very old " * 30
    last = "2026-11-03 10:01:00.000 | WARNING | app.x | new record"
    (logs / "app.log").write_text(prefix + "\nolder traceback\n" + last)
    monkeypatch.setattr(diag, "_LOG_SCAN_BYTES", len(last) + 20)
    data = search()["data"]
    assert data["records"] == [last] and data["matched_in_scan"] == 1
    assert not data["scan_complete"] and data["truncated"]


@pytest.mark.skipif(not sandbox.sandbox_supported(), reason="需要可运行 Bubblewrap 的 Linux 隔离工作区")
@pytest.mark.parametrize("owner", [WEB, TG])
def test_diagnostics_native_scripts_keep_host_identity_and_full_file_results(logs, monkeypatch, owner):
    monkeypatch.setattr(sandbox, "get_runtime_paths", lambda: SimpleNamespace(
        data_dir=logs, runtime_dir=logs / "runtime",
    ))

    async def check():
        store = InMemorySessionStateStore()
        context = await make_context(store, owner=owner)
        pipeline = ToolPipeline(catalog=ToolCatalog(), state_store=store)
        pipeline.catalog.register(sandbox.run_code_tool(pipeline))
        code = '''import json
from mf import diagnostics, activity
assert activity.actions()["items"] == []
assert set(diagnostics.runtime()) == {"agent_enabled", "telegram_enabled", "model_routing_enabled"}
try:
    diagnostics.query_db("SELECT value FROM settings_kv")
except RuntimeError:
    print("拒绝私有读取")
facts = {"db": diagnostics.query_db("SELECT '完整字段' AS reason"),
         "logs": diagnostics.search_logs(level="ERROR")}
json.dump(facts, open("diagnostics.json", "w"))
print(facts["db"]["data"]["rows"][0][0], len(facts["logs"]["data"]["records"]))
'''
        with patch.object(diag, "query_db", wraps=diag.query_db) as query_call, patch.object(
            diag, "search_logs", wraps=diag.search_logs,
        ) as log_call:
            result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": code}, context=context)
        data = result.outcome.public_content["data"]
        assert result.effect_plan is None
        if owner == TG:
            assert data["exit_code"] != 0
            assert "全局诊断仅对 Web 登录身份开放" in data["stderr"]
            query_call.assert_not_called()
            log_call.assert_not_called()
            return
        assert data["exit_code"] == 0, data
        assert data["stdout"].strip().splitlines() == ["拒绝私有读取", "完整字段 2"]
        assert [call["ok"] for call in data["sdk_calls"]] == [True, True, False, True, True]
        later = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code":
            'import json\nf=json.load(open("diagnostics.json"))\nprint(f["logs"]["data"]["matched_in_scan"])'}, context=context)
        assert later.outcome.public_content["data"]["stdout"].strip() == "2"
        assert later.outcome.public_content["data"]["sdk_calls"] == []

    with isolated_test_database():
        asyncio.run(check())


@pytest.mark.parametrize(("method", "arguments", "language", "owner"), [
    ("configuration", {}, "python", WEB), ("component", {"component": "qbittorrent"}, "shell", TG),
    ("pipeline", {"limit": 1, "offset": 0}, "python", WEB),
])
def test_new_diagnostics_native_rpc_keeps_full_local_facts_without_actions(tmp_path, monkeypatch, method, arguments, language, owner):
    if not sandbox.sandbox_supported():
        pytest.skip("需要可运行 Bubblewrap 的 Linux 隔离工作区")
    from app import config
    from tests.support import initialized_web_runtime

    with initialized_web_runtime():
        config.set_and_save({"QB_URL": "https://private-qb.invalid", "QB_API_KEY": "PRIVATE-QB-KEY", "TMDB_API_KEY": "PRIVATE-TMDB-KEY"})
        monkeypatch.setattr(sandbox, "get_runtime_paths", lambda: SimpleNamespace(data_dir=tmp_path, runtime_dir=tmp_path / "runtime"))
        async def check():
            store = InMemorySessionStateStore()
            context = await make_context(store, owner=owner)
            pipeline = ToolPipeline(catalog=ToolCatalog(), state_store=store)
            pipeline.catalog.register(sandbox.run_code_tool(pipeline))
            if language == "python":
                code = ('import json\nfrom mf import diagnostics\n'
                        f'value=diagnostics.{method}(**{arguments!r})\n'
                        'json.dump(value,open("diagnosis.json","w"),ensure_ascii=False)\nprint("complete")')
            else:
                code = "mf diagnostics.component --component qbittorrent > diagnosis.json"
            try:
                result = await pipeline.execute(sandbox.RUN_CODE_TOOL, {"code": code, "language": language}, context=context)
                assert result.outcome.public_content["data"]["exit_code"] == 0, result.outcome.public_content
                root, _ = sandbox._workspace_location(context.owner, context.session_id)
                full = json.loads((root / "work/diagnosis.json").read_text())
                assert full["data"]["network_accessed"] is False and full["evidence"]
                if method == "configuration":
                    assert len(full["data"]["components"]) == 6
                    assert all("fields" in row and "purpose" in row for row in full["data"]["components"])
                elif method == "component":
                    assert full["data"]["status"] == "ready" and full["data"]["fields"]
                else:
                    assert {"automation_database", "rss"} == set(full["data"]["sources"])
                assert result.effect_plan is None
                assert "PRIVATE-QB-KEY" not in json.dumps(full) and "PRIVATE-TMDB-KEY" not in json.dumps(full)
                assert "private-qb.invalid" not in json.dumps(full)
            finally:
                sandbox.clear_workspace(owner=owner, session_id=context.session_id)
        asyncio.run(check())


def test_new_diagnostics_permissions_keep_tg_allowed_and_unknown_principals_denied():
    for name, args in (("configuration", ()), ("component", ("tmdb",)), ("pipeline", ())):
        with patch.object(diag, name, return_value={"ok": True}) as read:
            for owner in (WEB, TG):
                with sdk.call_owner(owner):
                    assert getattr(sdk.diagnostics, name)(*args) == {"ok": True}
            with sdk.call_owner("api:untrusted"), pytest.raises(PermissionError):
                getattr(sdk.diagnostics, name)(*args)
        assert read.call_count == 2
