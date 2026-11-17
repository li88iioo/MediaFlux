"""媒体反代 Repository 契约。"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app import database as db
from app.database_schema import MEDIA_PROXY_BINDINGS_TABLE
from app.modules.media_server_profiles import media_proxy_binding_identity
from app.repositories import media_proxy as repository
from tests.support import isolated_test_database


class MediaProxyRepositoryTests(unittest.TestCase):

    def test_repository_prefers_exact_binding_and_only_falls_back_to_explicit_default(self):
        with isolated_test_database("media-proxy-repository.db"):
            instance_id = repository.add_media_proxy_instance(
                name="Repository",
                server_type="emby",
                upstream_url="http://127.0.0.1:8096",
                api_key="secret",
                listen_host="127.0.0.1",
                listen_port=18096,
                trust_forwarded_headers=1,
                trusted_proxy_cidrs_json='["172.18.0.1/32"]',
            )
            instance = repository.get_media_proxy_instance(instance_id)
            assert instance is not None
            self.assertEqual(int(instance["trust_forwarded_headers"]), 1)
            self.assertEqual(
                instance["trusted_proxy_cidrs_json"],
                '["172.18.0.1/32"]',
            )
            self.assertTrue(repository.update_media_proxy_instance(
                instance_id,
                {
                    "trust_forwarded_headers": 0,
                    "trusted_proxy_cidrs_json": '["192.168.88.110/32"]',
                },
            ))
            instance = repository.get_media_proxy_instance(instance_id)
            assert instance is not None
            self.assertEqual(int(instance["trust_forwarded_headers"]), 0)
            self.assertEqual(
                instance["trusted_proxy_cidrs_json"],
                '["192.168.88.110/32"]',
            )
            binding = repository.create_media_proxy_binding(
                instance_id=instance_id,
                media_item_id="item-1",
                media_source_id="source-1",
                source_type="local",
                local_relative_path="Movie.mkv",
            )
            identity = binding["upstream_identity"]
            self.assertIsNone(repository.get_media_proxy_binding(
                instance_id, ("item-1",), ("missing-source",), upstream_identity=identity,
            ))
            default = repository.create_media_proxy_binding(
                instance_id=instance_id, media_item_id="item-1", media_source_id="",
                source_type="guangya", guangya_file_id="default-file",
            )
            for source, expected in (("source-1", binding), ("missing-source", default), ("", default)):
                with self.subTest(source=source):
                    selected = repository.get_media_proxy_binding(
                        instance_id, ("item-1",), (source,), upstream_identity=identity,
                    )
                    assert selected is not None
                    self.assertEqual(selected["id"], expected["id"])
            repository.record_media_proxy_playback_attempt(
                instance_id=instance_id,
                playback_session_key="delete-session",
                media_item_id="item-1",
                route_class="stream",
                method="GET",
                status_code=206,
                source="upstream",
            )
            self.assertTrue(repository.delete_media_proxy_instance(instance_id))
            self.assertEqual(repository.list_media_proxy_bindings(instance_id), [])
            self.assertEqual(
                repository.list_media_proxy_playback_records(instance_id=instance_id)["total"],
                0,
            )
            self.assertEqual(
                repository.list_media_proxy_playback_sessions(instance_id=instance_id)["total"],
                0,
            )

    def test_create_binding_returns_row_from_same_transaction(self):
        with isolated_test_database("media-proxy-create-binding.db"):
            instance_id = repository.add_media_proxy_instance(
                name="Atomic binding",
                server_type="jellyfin",
                upstream_url="http://127.0.0.1:8096",
                api_key="",
                listen_host="127.0.0.1",
                listen_port=18097,
            )
            row = repository.create_media_proxy_binding(
                instance_id=instance_id,
                media_item_id="item-atomic",
                media_source_id="source-atomic",
                source_type="guangya",
                guangya_file_id="file-atomic",
            )
            self.assertGreater(int(row["id"]), 0)
            self.assertEqual(int(row["instance_id"]), instance_id)
            self.assertEqual(row["media_item_id"], "item-atomic")
            self.assertEqual(row["guangya_file_id"], "file-atomic")

    def test_migration_preserves_unverified_binding_and_allows_scoped_recreation(self):
        with isolated_test_database():
            instance_id = repository.add_media_proxy_instance(
                name="Historical", server_type="jellyfin", upstream_url="http://fixture.invalid",
                api_key="fixture", listen_host="127.0.0.1", listen_port=18096,
            )
            binding = dict(
                instance_id=instance_id, media_item_id="item", media_source_id="",
                source_type="guangya", guangya_file_id="old-file",
            )
            with db.get_conn() as conn:
                conn.execute("DROP TABLE media_proxy_bindings")
                conn.execute(MEDIA_PROXY_BINDINGS_TABLE
                             .replace("    upstream_identity TEXT NOT NULL DEFAULT '',\n", "")
                             .replace("instance_id, upstream_identity, media_item_id", "instance_id, media_item_id"))
                conn.execute(
                    "INSERT INTO media_proxy_bindings(id,instance_id,media_item_id,source_type,guangya_file_id,created_at,updated_at) "
                    "VALUES(7,?,'item','guangya','old-file','original','original')",
                    (instance_id,),
                )
                conn.execute("UPDATE sqlite_sequence SET seq=99 WHERE name='media_proxy_bindings'")
                conn.execute("PRAGMA user_version=42")
            db.init_db()
            historic = repository.list_media_proxy_bindings(instance_id)[0]
            self.assertEqual((historic["id"], historic["guangya_file_id"], historic["created_at"]), (7, "old-file", "original"))
            first = repository.create_media_proxy_binding(**binding)
            self.assertGreater(first["id"], 99)
            repository.update_media_proxy_instance(instance_id, {"upstream_url": "http://replacement.invalid"})
            instance = repository.get_media_proxy_instance(instance_id)
            assert instance is not None
            identity = media_proxy_binding_identity(instance)
            self.assertIsNone(repository.get_media_proxy_binding(instance_id, ("item",), upstream_identity=identity))
            second = repository.create_media_proxy_binding(**{**binding, "guangya_file_id": "new-file"})
            selected = repository.get_media_proxy_binding(instance_id, ("item",), upstream_identity=identity)
            assert selected is not None
            self.assertEqual(selected["guangya_file_id"], "new-file")
            self.assertEqual([row["id"] for row in repository.list_media_proxy_bindings(instance_id)], [7, first["id"], second["id"]])

    def test_record_contract_and_redaction_remain_behind_facade(self):
        with isolated_test_database("media-proxy-record-repository.db"):
            record_id = repository.record_media_proxy_playback_attempt(
                instance_id=3,
                route_class="video",
                method="get",
                status_code=502,
                source="upstream",
                failure_stage="proxy",
                error="https://example.invalid/?api_key=secret\nAuthorization: Bearer hidden",
            )
            payload = repository.list_media_proxy_playback_records(instance_id=3)
            self.assertEqual(payload["total"], 1)
            self.assertEqual(payload["items"][0]["id"], record_id)
            error = payload["items"][0]["error"]
            self.assertNotIn("example.invalid", error)
            self.assertNotIn("secret", error)
            self.assertNotIn("hidden", error)

    def test_record_cleanup_runs_periodically_instead_of_on_every_write(self):
        with isolated_test_database("media-proxy-maintenance.db"):
            prune_key = (
                f"{db.resolve_db_path()}:"
                f"{repository.datetime.now().strftime('%Y-%m-%d')}"
            )
            with (
                patch.object(
                    repository,
                    "_last_media_proxy_record_prune_key",
                    prune_key,
                ),
                patch.object(
                    repository,
                    "_media_proxy_record_writes_since_prune",
                    0,
                ),
                patch.object(
                    repository,
                    "_MEDIA_PROXY_RECORD_MAINTENANCE_INTERVAL",
                    2,
                ),
                patch.object(
                    repository,
                    "_delete_orphan_media_proxy_playback_sessions",
                    wraps=repository._delete_orphan_media_proxy_playback_sessions,
                ) as cleanup,
            ):
                for _ in range(2):
                    repository.record_media_proxy_playback_attempt(
                        instance_id=3,
                        route_class="video",
                        method="GET",
                        status_code=302,
                        source="guangya",
                    )

            self.assertEqual(cleanup.call_count, 1)

    def test_periodic_low_watermark_keeps_the_record_limit_strict(self):
        with isolated_test_database("media-proxy-record-limit.db"):
            with (
                patch.object(
                    repository,
                    "_last_media_proxy_record_prune_key",
                    "",
                ),
                patch.object(
                    repository,
                    "_media_proxy_record_writes_since_prune",
                    0,
                ),
                patch.object(
                    repository,
                    "_MEDIA_PROXY_RECORD_MAX_ROWS",
                    5,
                ),
                patch.object(
                    repository,
                    "_MEDIA_PROXY_RECORD_MAINTENANCE_INTERVAL",
                    2,
                ),
            ):
                for _ in range(12):
                    repository.record_media_proxy_playback_attempt(
                        instance_id=3,
                        route_class="video",
                        method="GET",
                        status_code=302,
                        source="guangya",
                    )
                    with db.get_conn() as conn:
                        total = conn.execute(
                            "SELECT COUNT(*) AS total "
                            "FROM media_proxy_playback_records"
                        ).fetchone()["total"]
                    self.assertLessEqual(total, 5)


if __name__ == "__main__":
    unittest.main()
