from __future__ import annotations

import hashlib
import hmac
import time

from app import database as db
from app.modules.web_secret import get_web_secret
from tests.support import IsolatedDatabaseTestCase


class AgentPrivacyLifecycleTests(IsolatedDatabaseTestCase):
    @staticmethod
    def _digest(domain: bytes, value: str) -> str:
        return hmac.new(
            get_web_secret().encode("utf-8"), domain + value.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def test_subject_purge_clears_all_agent_partitions(self):
        owner = "web:test-owner"
        principal = "principal:test-owner"
        now = db.now()
        digests = {
            "action": self._digest(b"mediaflux-agent-action-history:v1\0", owner),
            "session": self._digest(b"mediaflux-agent-session-context:v1\0", owner),
            "confirmation": self._digest(b"mediaflux-agent-confirmation:v1\0", owner),
            "job": self._digest(b"mediaflux-agent-durable-job:v1\0", owner),
            "workflow": self._digest(b"mediaflux-agent-missing-workflow:v1\0", owner),
            "tg_action": self._digest(b"mediaflux-telegram-agent-action:v1\0", owner),
            "tg_confirm": self._digest(b"mediaflux-telegram-write-confirmation:v1\0", owner),
            "conversation": self._digest(
                b"mediaflux-agent-conversation-principal:v1\0", principal
            ),
        }
        with db.get_conn() as conn:
            conn.execute(
                "INSERT INTO agent_action_history(owner_digest,tool_name,risk,status,ok,summary,started_at,finished_at) VALUES(?,?,?,?,?,?,?,?)",
                (digests["action"], "demo", "read", "ok", 1, "done", now, now),
            )
            conn.execute(
                "INSERT INTO agent_session_context(owner_digest,context_type,payload,expires_at,created_at) VALUES(?,?,?,?,?)",
                (digests["session"], "latest_tool", "{}", time.time() + 60, now),
            )
            conn.execute(
                "INSERT INTO agent_session_context_epochs("
                "owner_digest,context_type,generation,touched_at,updated_at"
                ") VALUES(?,?,?,?,?)",
                (digests["session"], "directory_scrape", 1, time.time(), now),
            )
            conn.execute(
                "INSERT INTO agent_confirmation_epochs(owner_digest,generation,touched_at,updated_at) VALUES(?,?,?,?)",
                (digests["confirmation"], 1, time.time(), now),
            )
            conn.execute(
                "INSERT INTO agent_confirmations(confirmation_id,owner_digest,tool_name,expires_at,owner_generation,created_at) VALUES(?,?,?,?,?,?)",
                ("confirm_privacy_0001", digests["confirmation"], "demo", time.time() + 60, 1, now),
            )
            conn.execute(
                "INSERT INTO agent_jobs(job_id,owner_digest,job_type,dedupe_key,status,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                ("job_privacy_00000001", digests["job"], "library_episode_audit", "one", "pending", now, now, now),
            )
            conn.execute(
                "INSERT INTO organize_operation_jobs("
                "job_id,job_kind,owner_digest,operation,reference,payload_json,"
                "dedupe_digest,status,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "a" * 32, "agent_directory_scrape", digests["job"],
                    "目录刮削", "", "{}", "b" * 64, "pending", now, now,
                ),
            )
            conn.execute(
                "INSERT INTO agent_missing_media_workflows(workflow_id,owner_digest,source_tool,title,tmdb_id,season,as_of,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("workflow_privacy_0001", digests["workflow"], "library.search_missing_episode_resources", "Demo", "1", 1, "2026-08-22", "search_ready", now, now),
            )
            conn.execute(
                "INSERT INTO telegram_agent_actions(action_id,owner_digest,action_kind,group_id,expires_at,created_at) VALUES(?,?,?,?,?,?)",
                ("tg_action_privacy_1", digests["tg_action"], "result", "g1", time.time() + 60, now),
            )
            conn.execute(
                "INSERT INTO telegram_write_confirmations(action_id,group_id,owner_digest,decision,operation,expires_at,created_at) VALUES(?,?,?,?,?,?,?)",
                ("tg_confirm_privacy_1", "g1", digests["tg_confirm"], "yes", "demo", time.time() + 60, now),
            )
            conn.execute(
                "INSERT INTO agent_conversations(principal_digest,session_id,title,created_at,updated_at) VALUES(?,?,?,?,?)",
                (digests["conversation"], "privacy_session_0001", "Demo", now, now),
            )
            conn.execute(
                "INSERT INTO agent_conversation_epochs(principal_digest,session_id,generation,updated_at) VALUES(?,?,?,?)",
                (digests["conversation"], "privacy_session_0001", 1, now),
            )

        deleted = db.purge_agent_subject_data(owner=owner, principal=principal)
        self.assertTrue(all(value == 1 for value in deleted.values()), deleted)

    def test_ttl_cleanup_only_removes_terminal_records_and_maintenance_runs(self):
        old = "2026-07-01 00:00:00"
        now = "2026-08-22 00:00:00"
        with db.get_conn() as conn:
            for suffix, status in (("done", "succeeded"), ("active", "running")):
                conn.execute(
                    "INSERT INTO agent_jobs(job_id,owner_digest,job_type,dedupe_key,status,next_run_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (f"job_ttl_{suffix}_0001", "a" * 64, "library_episode_audit", suffix, status, now, old, old),
                )
            for suffix, state in (("done", "visible"), ("active", "verification_pending")):
                conn.execute(
                    "INSERT INTO agent_missing_media_workflows(workflow_id,owner_digest,source_tool,title,tmdb_id,season,as_of,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (f"workflow_ttl_{suffix}", "b" * 64, "library.search_missing_episode_resources", "Demo", "1", 1, "2026-07-01", state, old, old),
                )
        with db.get_conn() as conn:
            for suffix, status in (("done", "completed"), ("active", "running")):
                conn.execute(
                    "INSERT INTO organize_operation_jobs("
                    "job_id,job_kind,owner_digest,operation,reference,payload_json,"
                    "dedupe_digest,status,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        ("c" if suffix == "done" else "d") * 32,
                        "agent_directory_scrape", "e" * 64, "目录刮削", "", "{}",
                        ("f" if suffix == "done" else "1") * 64, status, old, old,
                    ),
                )
        result = db.purge_expired_agent_task_history(
            current_time=now, next_cleanup_at="2026-08-23 00:00:00",
            terminal_before="2026-08-01 00:00:00", limit_per_table=50,
        )
        self.assertEqual(result["jobs"], 1)
        self.assertEqual(result["organize_operation_jobs"], 1)
        self.assertEqual(result["missing_media_workflows"], 1)
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_jobs").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM organize_operation_jobs").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_missing_media_workflows").fetchone()[0], 1)
        maintenance = db.maintain_sqlite_database(incremental_pages=1)
        self.assertTrue(maintenance["optimized"])
        self.assertGreaterEqual(maintenance["freelist_before"], maintenance["freelist_after"])
