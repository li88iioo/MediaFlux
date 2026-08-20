"""媒体反代 Repository 与 app.database 兼容门面契约。"""
from __future__ import annotations

import unittest

from app import database as db
from app.repositories import media_proxy as repository
from tests.support import isolated_test_database


class MediaProxyRepositoryTests(unittest.TestCase):
    def test_database_facade_reexports_repository_objects(self):
        for name in (
            "add_media_proxy_instance",
            "list_media_proxy_instances",
            "get_media_proxy_instance",
            "update_media_proxy_instance",
            "delete_media_proxy_instance",
            "add_media_proxy_binding",
            "create_media_proxy_binding",
            "list_media_proxy_bindings",
            "get_media_proxy_binding",
            "delete_media_proxy_binding",
            "record_media_proxy_playback_attempt",
            "list_media_proxy_playback_records",
            "list_media_proxy_playback_sessions",
            "clear_media_proxy_playback_records",
        ):
            self.assertIs(getattr(db, name), getattr(repository, name))

    def test_repository_uses_active_test_database_and_preserves_binding_fallback(self):
        with isolated_test_database("media-proxy-repository.db"):
            instance_id = repository.add_media_proxy_instance(
                name="Repository",
                server_type="emby",
                upstream_url="http://127.0.0.1:8096",
                api_key="secret",
                listen_host="127.0.0.1",
                listen_port=18096,
            )
            repository.add_media_proxy_binding(
                instance_id=instance_id,
                media_item_id="item-1",
                media_source_id="source-1",
                source_type="local",
                local_relative_path="Movie.mkv",
            )
            row = repository.get_media_proxy_binding(instance_id, "item-1", "missing-source")
            self.assertIsNotNone(row)
            self.assertEqual(row["media_source_id"], "source-1")
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

    def test_record_contract_and_redaction_remain_behind_facade(self):
        with isolated_test_database("media-proxy-record-repository.db"):
            record_id = db.record_media_proxy_playback_attempt(
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


if __name__ == "__main__":
    unittest.main()
