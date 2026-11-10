"""真实 SQLite/ZIP 恢复后，通知规则与四源摘要仍对应原业务历史。"""

from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app import database as db
from app.repositories import rss as rss_repo
from app.repositories import media_subscriptions as media_subscriptions_repo
from app.modules import backup
from app.repositories import media_experience as repository
from app.runtime_paths import RuntimePaths
from tests.support import isolated_test_database


class MediaConsumptionRestoreAuditTests(unittest.TestCase):
    @staticmethod
    def _paths(database_path):
        root = database_path.parent
        paths = RuntimePaths(
            root / "program",
            root,
            root,
            root / "cache",
            root / "logs",
            root / "strm",
            root / "trash",
        )
        paths.ensure_writable_dirs()
        paths.env_file.write_text("WEB_PORT=1258\n", encoding="utf-8")
        return paths

    @staticmethod
    def _seed():
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d 12:00:00")
        sid = media_subscriptions_repo.add_media_subscription(
            provider="tmdb",
            external_id="444",
            tmdb_id="444",
            media_type="tv",
            title="Restored subscription",
            action="confirm",
            download_target="guangya",
            sites=("mikan",),
        )
        rss_id = rss_repo.add_rss_subscription("restored", "https://fixture.invalid/rss")
        current = repository.get_notification_rule(sid)
        assert current is not None
        repository.set_notification_rule(
            sid,
            expected_rule_revision=0,
            expected_subscription_revision=current["subscription_revision"],
            updates={"enabled": True},
        )
        with db.get_conn() as conn:
            source = conn.execute(
                "INSERT INTO local_media_sources(name,local_root,created_at,updated_at) VALUES('history','/fixture',?,?)",
                (stamp, stamp),
            ).lastrowid
            conn.execute(
                "INSERT INTO local_media_tasks(source_id,content_path,status,operation_token,title,created_at,updated_at,completed_at) VALUES(?,'/fixture/video','completed','saved-operation','Restored local',?,?,?)",
                (source, stamp, stamp, stamp),
            )
            conn.execute(
                "INSERT INTO media_subscription_runs(subscription_id,status,started_at,finished_at) VALUES(?,'satisfied',?,?)",
                (sid, stamp, stamp),
            )
            conn.execute(
                "INSERT INTO rss_entries(rss_item_id,title,status,created_at,processed_at,submitted_at) VALUES(?,'Restored RSS','pending',?,'','')",
                (rss_id, stamp),
            )
            conn.executemany(
                "INSERT INTO download_log(source,title,status,created_at,updated_at,completed_at) VALUES('qb',?,'success',?,'','')",
                [(f"Restored download {i}", stamp) for i in range(80)],
            )
        return sid

    @staticmethod
    def _business_summary():
        summary = repository.today_content_summary()
        return {
            key: value
            for key, value in summary.items()
            if key not in {"as_of", "timezone"}
        }

    @staticmethod
    def _change(sid):
        current = repository.get_notification_rule(sid)
        assert current is not None
        repository.set_notification_rule(
            sid,
            expected_rule_revision=current["revision"],
            expected_subscription_revision=current["subscription_revision"],
            updates={"enabled": False},
        )
        with db.get_conn() as conn:
            conn.execute("UPDATE download_log SET status='failed'")

    def test_full_restore_keeps_ids_rule_and_all_four_source_results(self):
        with isolated_test_database("mediaflux.db") as path:
            paths = self._paths(path)
            sid = self._seed()
            rule = repository.get_notification_rule(sid)
            expected = self._business_summary()
            self.assertEqual(expected["event_count"], 83)
            with db.get_conn() as conn:
                history = [
                    dict(row)
                    for row in conn.execute("SELECT * FROM download_log ORDER BY id")
                ]
            archive = backup.create_backup(paths)
            self._change(sid)
            self.assertNotEqual(self._business_summary(), expected)
            backup.restore_backup(paths, archive)
            db.configure_database(path, test_mode=True)
            db.init_db()
            self.assertFalse(backup.recover_pending_restore(paths))
            self.assertEqual(repository.get_notification_rule(sid), rule)
            self.assertEqual(self._business_summary(), expected)
            with db.get_conn() as conn:
                self.assertEqual(
                    [
                        dict(row)
                        for row in conn.execute(
                            "SELECT * FROM download_log ORDER BY id"
                        )
                    ],
                    history,
                )
                self.assertEqual(
                    conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                )
                self.assertEqual(
                    conn.execute("PRAGMA foreign_key_check").fetchall(), []
                )

    def test_interrupted_restore_recovers_previous_live_business_state_once(self):
        with isolated_test_database("mediaflux.db") as path:
            paths = self._paths(path)
            sid = self._seed()
            archive = backup.create_backup(paths)
            self._change(sid)
            rule = repository.get_notification_rule(sid)
            expected = self._business_summary()
            replace = backup.os.replace
            interrupted = False

            def replace_then_interrupt(source, destination):
                nonlocal interrupted
                result = replace(source, destination)
                if (
                    not interrupted
                    and Path(destination) == path
                    and Path(source).name.endswith(".tmp")
                ):
                    interrupted = True
                    raise KeyboardInterrupt(
                        "synthetic interruption after database replacement"
                    )
                return result

            with patch.object(backup.os, "replace", replace_then_interrupt):
                with self.assertRaises(KeyboardInterrupt):
                    backup.restore_backup(paths, archive)
            self.assertTrue(interrupted)
            self.assertTrue(backup.recover_pending_restore(paths))
            db.configure_database(path, test_mode=True)
            db.init_db()
            self.assertEqual(repository.get_notification_rule(sid), rule)
            self.assertEqual(self._business_summary(), expected)
            self.assertFalse(backup.recover_pending_restore(paths))

    def test_missing_historical_rule_stays_default_without_rewriting_legacy_dates(self):
        with isolated_test_database("mediaflux.db") as path:
            paths = self._paths(path)
            sid = self._seed()
            current = repository.get_notification_rule(sid)
            assert current is not None
            repository.reset_notification_rule(
                sid,
                expected_rule_revision=current["revision"],
                expected_subscription_revision=current["subscription_revision"],
            )
            expected_rule = repository.get_notification_rule(sid)
            archive = backup.create_backup(paths)
            self._change(sid)
            backup.restore_backup(paths, archive)
            db.configure_database(path, test_mode=True)
            db.init_db()
            self.assertEqual(repository.get_notification_rule(sid), expected_rule)
            self.assertEqual(self._business_summary()["event_count"], 83)
            with db.get_conn() as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM media_subscription_notification_rules"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    tuple(
                        conn.execute(
                            "SELECT processed_at,submitted_at FROM rss_entries"
                        ).fetchone()
                    ),
                    ("", ""),
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM download_log WHERE completed_at='' AND updated_at=''"
                    ).fetchone()[0],
                    80,
                )
