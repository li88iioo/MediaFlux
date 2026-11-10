"""刷新交接故障注入：不接触真实网盘或媒体服务器。"""

from typing import cast
from typing import Any
from app.modules.local_media_service import LocalMovePlan
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.repositories import strm as strm_repo
from app.modules.local_media_service import LocalMediaService
from app.modules.media_refresh_coordinator import MediaRefreshCoordinator
from app.modules.scheduler import STRMScheduler
from app.modules.strm_metadata_worker import STRMMetadataWorker
from app.modules.strm import STRM_SUBDIR
from app.repositories.media_refresh_queue import (
    claim_due_media_refreshes, clear_media_refresh_queue, enqueue_media_refresh,
    media_refresh_queue_status,
)
from tests.support import IsolatedDatabaseTestCase


class RefreshHandoffRecoveryTests(IsolatedDatabaseTestCase):
    def setUp(self):
        clear_media_refresh_queue()
        self.addCleanup(patch.stopall)
        patch("socket.socket.connect", side_effect=AssertionError("network forbidden")).start()

    def test_bound_refresh_survives_folder_query_outage_and_worker_restart(self):
        client = Mock(display_name="Jellyfin")
        client.list_virtual_folders.side_effect = TimeoutError("fixture timeout")
        client.refresh_for_paths.return_value = {"ok": True}
        plan = SimpleNamespace(
            provider="jellyfin", library_id="movies", library_name="电影",
            target=Path("/media/Movies/Film/Film.mkv"),
        )
        profile = SimpleNamespace(server_type="jellyfin", label="Jellyfin", enabled=True,
                                  configured=True, url="http://invalid", credential="test")
        with patch("app.modules.media_server_profiles.list_configured_profiles", return_value=[profile]), \
             patch("app.clients.jellyfin.JellyfinClient", return_value=client), \
             patch("app.modules.media_refresh_coordinator.get_media_refresh_coordinator"):
            warnings = LocalMediaService._refresh_plans(cast(list[LocalMovePlan], [plan]))
        self.assertEqual(warnings, [])
        client.list_virtual_folders.assert_not_called()
        self.assertEqual(media_refresh_queue_status()["paths"], 1)

        worker = MediaRefreshCoordinator()
        group = claim_due_media_refreshes(owner=worker._owner, force=True)[0]
        with patch.object(worker, "_client_for", return_value=client):
            worker._process_group(group)
        self.assertEqual(media_refresh_queue_status()["paths"], 1)
        client.refresh_for_paths.assert_not_called()

        client.list_virtual_folders.side_effect = None
        client.list_virtual_folders.return_value = [{"id": "movies", "name": "电影"}]
        worker = MediaRefreshCoordinator()  # 只重试刷新，不重新整理或移动。
        group = claim_due_media_refreshes(owner=worker._owner, force=True)[0]
        with patch.object(worker, "_client_for", return_value=client):
            worker._process_group(group)
        self.assertEqual(media_refresh_queue_status()["paths"], 0)
        client.refresh_for_paths.assert_called_once_with(
            ["/media/Movies/Film"], allowed_library_ids=("movies",),
            allow_global_fallback=False, skip_item_ids=(),
        )

    def test_old_outbox_ack_cannot_delete_new_same_path_event(self):
        path = "/fixture/Film"
        strm_repo.enqueue_strm_refresh_paths([path])
        old = strm_repo.list_strm_refresh_entries()
        strm_repo.enqueue_strm_refresh_paths([path])
        self.assertEqual(strm_repo.acknowledge_strm_refresh_paths(old), 0)
        self.assertEqual(strm_repo.count_strm_refresh_paths(), 1)
        current = strm_repo.list_strm_refresh_entries()
        self.assertNotEqual(old[0]["event_token"], current[0]["event_token"])
        self.assertEqual(strm_repo.acknowledge_strm_refresh_paths(current), 1)

    def test_old_outbox_ack_cannot_delete_reinserted_key_or_other_provider(self):
        path = "/fixture/Film"
        strm_repo.enqueue_strm_refresh_paths([path], allow_emby=False)
        old = strm_repo.list_strm_refresh_entries()
        self.assertEqual(strm_repo.acknowledge_strm_refresh_paths(old), 1)
        strm_repo.enqueue_strm_refresh_paths([path], allow_emby=False)
        strm_repo.enqueue_strm_refresh_paths([path], allow_emby=True)
        self.assertEqual(strm_repo.acknowledge_strm_refresh_paths(old), 0)
        self.assertEqual(strm_repo.count_strm_refresh_paths(), 2)
        current = strm_repo.list_strm_refresh_entries()
        self.assertEqual(strm_repo.acknowledge_strm_refresh_paths([current[0]]), 1)
        self.assertEqual(strm_repo.count_strm_refresh_paths(), 1)

    def test_old_worker_handoff_cannot_ack_a_new_failed_scheduler_handoff(self):
        path = f"/fixture/{STRM_SUBDIR}/Film/Film.strm"
        strm_repo.enqueue_strm_refresh_paths([str(Path(path).parent)])
        calls = []

        def handoff(paths, **kwargs):
            calls.append(paths)
            if len(calls) == 2:
                return {"Jellyfin": "failed"}
            newer = STRMScheduler._refresh_media_servers(
                changed_paths=[path], changed_dirs=[str(Path(path).parent)], immediate=True,
            )
            self.assertEqual(newer, {"Jellyfin": "failed"})
            return {"Jellyfin": "queued"}

        with ExitStack() as stack:
            stack.enter_context(patch("app.modules.scheduler.get", side_effect=lambda k, d="": "/fixture" if k == "STRM_ROOT" else d))
            stack.enter_context(patch("app.modules.media_refresh_coordinator.enqueue_media_refresh_paths", side_effect=handoff))
            STRMMetadataWorker()._flush_media_refresh(force=False)
        self.assertEqual(len(calls), 2)
        self.assertEqual(strm_repo.count_strm_refresh_paths(), 1)
        with patch("app.modules.media_refresh_coordinator.enqueue_media_refresh_paths", return_value={"Jellyfin": "queued"}):
            STRMMetadataWorker()._flush_media_refresh(force=True)
        self.assertEqual(strm_repo.count_strm_refresh_paths(), 0)

    def test_binding_constraints_survive_queue_and_fail_closed(self):
        cases = (
            ({"id": "missing", "name": "电影"}, [{"id": "movies", "name": "电影"}], False),
            ({"id": "movies", "name": "剧集"}, [{"id": "movies", "name": "电影"}], False),
            ({"id": "", "name": "电影"}, [{"id": "a", "name": "电影"}, {"id": "b", "name": "电影"}], False),
            ({"id": "", "name": "电影"}, [{"id": "movies", "name": "电影"}], True),
            ({"id": "movies", "name": ""}, [{"id": "movies", "name": "电影"}], True),
        )
        for provider in ("jellyfin", "emby"):
            for binding, folders, accepted in cases:
                with self.subTest(provider=provider, binding=binding, accepted=accepted):
                    clear_media_refresh_queue()
                    enqueue_media_refresh(provider, ["/media/Film"], library_binding=binding)
                    worker = MediaRefreshCoordinator()
                    client = Mock(display_name=provider)
                    client.list_virtual_folders.return_value = folders
                    client.refresh_for_paths.return_value = {"ok": True}
                    group = claim_due_media_refreshes(owner=worker._owner, force=True)[0]
                    self.assertEqual(group["library_binding"], binding)
                    with patch.object(worker, "_client_for", return_value=client):
                        worker._process_group(group)
                    self.assertEqual(client.refresh_for_paths.call_count, int(accepted))
                    self.assertEqual(media_refresh_queue_status()["paths"], int(not accepted))
                    client.refresh_all.assert_not_called()
                    if accepted:
                        self.assertEqual(client.refresh_for_paths.call_args.kwargs["allowed_library_ids"], ("movies",))

    def test_distinct_bindings_do_not_merge_and_disabled_provider_keeps_intent(self):
        for name in ("电影", "剧集"):
            enqueue_media_refresh("jellyfin", ["/media/Film"], library_binding={"id": "movies", "name": name})
        enqueue_media_refresh("jellyfin", ["/media/Film"])
        worker = MediaRefreshCoordinator()
        groups = claim_due_media_refreshes(owner=worker._owner, force=True)
        self.assertEqual(len(groups), 3)
        self.assertEqual(len({group["group_key"] for group in groups}), 3)
        with patch.object(worker, "_client_for", return_value=None):
            for group in groups:
                worker._process_group(group)
        self.assertEqual(media_refresh_queue_status()["paths"], 3)

    def test_ack_requires_token_and_supports_large_batches(self):
        entries = strm_repo.enqueue_strm_refresh_paths([f"/fixture/{i}" for i in range(1201)])
        for invalid in (["/fixture/0"], [{"path": "/fixture/0", "allow_emby": True}],
                        [entries[0], {"path": "bad", "event_token": ""}]):
            with self.assertRaises(ValueError):
                strm_repo.acknowledge_strm_refresh_paths(cast(list[dict[str, Any]], invalid))
        self.assertEqual(strm_repo.count_strm_refresh_paths(), 1201)
        self.assertEqual(strm_repo.acknowledge_strm_refresh_paths(entries), 1201)
