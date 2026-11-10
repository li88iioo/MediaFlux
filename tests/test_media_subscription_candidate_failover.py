"""SUB-02：真实订阅服务 / 选源 / 下载准入链路的离线换源回归。"""
from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.repositories import media_subscriptions as media_subscriptions_repo
from app.clients.http import HttpInvalidResponse
from app.indexers import downloads
from app.indexers.models import IndexerItem, IndexerPage, ResolvedDownload
from app.indexers.providers.base import IndexerAdapter
from app.indexers.registry import IndexerRegistry
from app.indexers.result_store import IndexerResultStore
from app.indexers.service import IndexerService
from app.modules import download_dispatcher, media_subscriptions
from app.modules.media_subscriptions import (
    MediaSubscriptionError,
    MediaSubscriptionService,
)
from tests.support import IsolatedDatabaseTestCase


class OfflineSource:
    """只替换资源站的外部检索 / resolver，保留真实聚合、结果存储和排序。"""

    site_id = "nyaa"
    site_name = "Offline source"
    default_enabled = True

    def __init__(self, items):
        self.items = items
        self.fail_titles = set()
        self.resolve_titles = []
        self.before_resolve = None
        self.resolved_by_title = {}

    async def search(self, request):
        return IndexerPage(items=list(self.items), page=request.page, has_more=False, pagination_supported=True)

    async def resolve(self, item):
        self.resolve_titles.append(item.title)
        if self.before_resolve is not None:
            self.before_resolve(item)
        if item.title in self.fail_titles:
            raise HttpInvalidResponse("invalid private upstream response")
        return self.resolved_by_title.get(item.title, ResolvedDownload("magnet", item.magnet))

    def http_client_for_result(self, _item):
        return SimpleNamespace(max_response_bytes=2 * 1024 * 1024)


class MediaSubscriptionCandidateFailoverTests(IsolatedDatabaseTestCase):
    def setUp(self):
        with db.get_conn() as conn:
            for table in (
                "media_download_admissions", "media_subscription_candidates",
                "media_subscription_runs", "media_subscriptions", "download_requests",
            ):
                conn.execute(f"DELETE FROM {table}")

    def scenario(self, media_type="movie", *, episode_count=1, failures=(0,)):
        title = "Candidate Movie" if media_type == "movie" else "Candidate Series"
        subscription_id = media_subscriptions_repo.add_media_subscription(
            provider="tmdb", external_id="86034", tmdb_id="86034",
            media_type=media_type, title=title, original_title=title,
            year="2020", action="auto", download_target="qb", sites=["nyaa"],
        )
        items = []
        published = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for episode in range(1, episode_count + 1):
            position = "2020" if media_type == "movie" else f"S01E{episode:02d}"
            for choice in ("First", "Second"):
                index = len(items)
                items.append(IndexerItem(
                    site_id="nyaa", site_name="Offline source",
                    title=f"{title} {position} 1080p WEB-DL {choice}",
                    detail_url=f"https://fixture.invalid/{index}",
                    download_state="ready", download_kinds=("magnet", "torrent"),
                    magnet=f"magnet:?xt=urn:btih:{index + 1:040x}",
                    seeders=100 - index,
                    published_at=published - timedelta(seconds=index),
                ))
        source = OfflineSource(items)
        source.fail_titles = {items[index].title for index in failures}
        indexer = IndexerService(
            registry=IndexerRegistry(cast(dict[str, IndexerAdapter], {"nyaa": source})),
            result_store=IndexerResultStore(),
        )
        self.addCleanup(lambda: asyncio.run(indexer.aclose()))
        self.enterContext(patch.object(media_subscriptions, "get_indexer_service", return_value=indexer))
        original_bool = media_subscriptions.config.get_bool
        self.enterContext(patch.object(
            media_subscriptions.config, "get_bool",
            side_effect=lambda key, default=False: True if key == "INDEXER_SEARCH_ENABLED" else original_bool(key, default),
        ))
        self.enterContext(patch.object(media_subscriptions, "_tmdb_detail", return_value={
            "title": title, "name": title, "original_title": title, "original_name": title,
            "release_date": "2020-01-01", "seasons": [{"season_number": 1}],
        }))
        self.enterContext(patch.object(media_subscriptions, "TMDBClient", return_value=SimpleNamespace(
            tv_season_detail=lambda *_: {"episodes": [
                {"episode_number": episode, "air_date": "2020-01-01"}
                for episode in range(1, episode_count + 1)
            ]}, close=lambda: None,
        )))
        self.enterContext(patch.object(media_subscriptions, "inspect_media_identity_sources", return_value=[{
            "server_type": "jellyfin", "status": "ready", "present": False,
        }]))
        self.enterContext(patch.object(media_subscriptions, "inspect_series_episode_sources", return_value=[{
            "server_type": "jellyfin", "status": "ready", "episodes": [],
        }]))
        backend = self.enterContext(patch.object(
            download_dispatcher, "_submit_qb", return_value={"ok": True, "task_id": "offline-qb"},
        ))
        self.enterContext(patch.object(
            download_dispatcher, "_submit_guangya", side_effect=AssertionError("unexpected guangya backend"),
        ))
        return subscription_id, source, indexer, backend

    @staticmethod
    def admissions(subscription_id):
        with db.get_conn() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM media_download_admissions WHERE subscription_id=? ORDER BY id",
                (subscription_id,),
            )]

    @staticmethod
    def request_count():
        with db.get_conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0]

    def assert_check_persisted(self, subscription_id, *, candidates, submitted, missing):
        row = media_subscriptions_repo.get_media_subscription(subscription_id)
        assert row is not None
        payload = json.loads(row["result_json"])
        run = media_subscriptions_repo.list_media_subscription_runs(subscription_id=subscription_id)[0]
        self.assertEqual((row["status"], run["status"]), ("missing", "missing"))
        self.assertEqual((payload["candidate_count"], payload["auto_submitted"]), (candidates, submitted))
        self.assertEqual(int(row["missing_count"]), missing)
        self.assertEqual(len(json.loads(row["missing_json"])), missing)
        self.assertEqual(row["last_error"], "")
        return payload

    def test_movie_resolver_failure_tries_healthy_candidate_and_persists_run(self):
        subscription_id, source, _indexer, backend = self.scenario()
        checked = asyncio.run(MediaSubscriptionService().check_subscription(subscription_id))
        self.assertEqual(checked["result"]["auto_submitted"], 1)
        self.assertEqual(source.resolve_titles, [item.title for item in source.items])
        backend.assert_called_once()
        admissions = self.admissions(subscription_id)
        self.assertEqual(len(admissions), 2)
        self.assertEqual((admissions[0]["status"], admissions[0]["request_id"]), ("failed", None))
        self.assertTrue(admissions[0]["completed_at"])
        self.assertTrue(admissions[1]["request_id"])
        self.assertEqual(self.request_count(), 1)
        first = media_subscriptions_repo.get_media_subscription_candidate(admissions[0]["candidate_id"])
        second = media_subscriptions_repo.get_media_subscription_candidate(admissions[1]["candidate_id"])
        assert first is not None
        self.assertEqual((first["status"], first["request_id"]), ("available", None))
        assert second is not None
        self.assertEqual((second["status"], second["request_id"]), ("submitted", admissions[1]["request_id"]))
        self.assert_check_persisted(subscription_id, candidates=2, submitted=1, missing=1)

    def test_movie_all_candidates_fail_before_submission_finishes_missing_run(self):
        subscription_id, source, _indexer, backend = self.scenario(failures=(0, 1))
        checked = asyncio.run(MediaSubscriptionService().check_subscription(subscription_id))
        self.assertEqual(checked["result"]["auto_submitted"], 0)
        self.assertEqual(len(source.resolve_titles), 2)
        backend.assert_not_called()
        self.assertEqual([(row["status"], row["request_id"]) for row in self.admissions(subscription_id)],
                         [("failed", None), ("failed", None)])
        self.assertEqual(media_subscriptions_repo.list_active_media_download_admissions(subscription_id), [])
        self.assertEqual(self.request_count(), 0)
        self.assert_check_persisted(subscription_id, candidates=2, submitted=0, missing=1)

    def test_tv_exhausted_episode_continues_next_missing_episode_and_rotation(self):
        subscription_id, source, _indexer, backend = self.scenario(
            "tv", episode_count=2, failures=(0, 1, 2),
        )
        checked = asyncio.run(MediaSubscriptionService().check_subscription(subscription_id))
        self.assertEqual(checked["result"]["auto_submitted"], 1)
        self.assertEqual(source.resolve_titles, [item.title for item in source.items])
        backend.assert_called_once()
        admissions = self.admissions(subscription_id)
        self.assertEqual([(row["episode"], row["status"], row["request_id"]) for row in admissions[:3]],
                         [(1, "failed", None), (1, "failed", None), (2, "failed", None)])
        self.assertEqual(admissions[3]["episode"], 2)
        self.assertTrue(admissions[3]["request_id"])
        payload = self.assert_check_persisted(subscription_id, candidates=4, submitted=1, missing=2)
        self.assertEqual(payload["_search_rotation"], {"revision": 1, "season": 1, "episode": 2})
        self.assertNotIn("_search_rotation", checked["result"])

    def assert_failure_stops_round(self, subscription_id, source, backend, *, submitted):
        with self.assertRaises(MediaSubscriptionError) as raised:
            asyncio.run(MediaSubscriptionService().check_subscription(subscription_id))
        self.assertEqual(raised.exception.code, "download_failed")
        self.assertFalse(raised.exception.can_try_next_candidate)
        self.assertEqual(source.resolve_titles, [source.items[0].title])
        self.assertEqual(backend.call_count, int(submitted))
        self.assertEqual(media_subscriptions_repo.list_media_subscription_runs(subscription_id=subscription_id)[0]["status"], "failed")
        get_media_subscription_result = media_subscriptions_repo.get_media_subscription(subscription_id)
        assert get_media_subscription_result is not None
        self.assertEqual(get_media_subscription_result["status"], "error")
        return self.admissions(subscription_id)

    def test_invalid_magnet_input_is_safe_to_try_next_candidate(self):
        subscription_id, source, _indexer, backend = self.scenario(failures=())
        source.resolved_by_title[source.items[0].title] = ResolvedDownload("magnet", "not-a-magnet")
        checked = asyncio.run(MediaSubscriptionService().check_subscription(subscription_id))
        self.assertEqual(checked["result"]["auto_submitted"], 1)
        self.assertEqual(source.resolve_titles, [item.title for item in source.items])
        backend.assert_called_once()
        admission = self.admissions(subscription_id)[0]
        self.assertEqual((admission["status"], admission["request_id"]), ("failed", None))
        self.assertEqual(admission["error"], HttpInvalidResponse.default_public_message)

    def test_invalid_torrent_input_is_safe_to_try_next_candidate(self):
        subscription_id, source, _indexer, backend = self.scenario(failures=())
        source.resolved_by_title[source.items[0].title] = ResolvedDownload("torrent", b"not-a-torrent")
        checked = asyncio.run(MediaSubscriptionService().check_subscription(subscription_id))
        self.assertEqual(checked["result"]["auto_submitted"], 1)
        self.assertEqual(source.resolve_titles, [item.title for item in source.items])
        backend.assert_called_once()
        first = self.admissions(subscription_id)[0]
        self.assertEqual((first["status"], first["request_id"]), ("failed", None))
        self.assertEqual(first["error"], "资源下载数据无效")

    def test_first_accepted_candidate_never_tries_second_candidate(self):
        subscription_id, source, _indexer, backend = self.scenario(failures=())
        checked = asyncio.run(MediaSubscriptionService().check_subscription(subscription_id))
        self.assertEqual(checked["result"]["auto_submitted"], 1)
        self.assertEqual(source.resolve_titles, [source.items[0].title])
        backend.assert_called_once()
        self.assertEqual(len(self.admissions(subscription_id)), 1)
        self.assertEqual(len(media_subscriptions_repo.list_active_media_download_admissions(subscription_id)), 1)

    def test_bound_backend_rejection_does_not_try_next_candidate_or_episode(self):
        subscription_id, source, _indexer, backend = self.scenario("tv", episode_count=2, failures=())
        backend.return_value = {"ok": False, "error": "definite backend rejection"}
        admissions = self.assert_failure_stops_round(subscription_id, source, backend, submitted=True)
        self.assertEqual(len(admissions), 1)
        self.assertEqual(admissions[0]["status"], "failed")
        self.assertTrue(admissions[0]["request_id"])
        request = download_requests_repo.get_download_request(admissions[0]["request_id"])
        assert request is not None
        self.assertEqual((request["status"], request["qb_status"]), ("failed", "failed"))

    def test_unknown_receipt_keeps_bound_admission_and_never_switches_candidate(self):
        subscription_id, source, _indexer, backend = self.scenario(failures=())
        backend.return_value = {
            "ok": False, "failure_code": "qb_outcome_unknown", "outcome_unknown": True,
            "error": "receipt unknown",
        }
        checked = asyncio.run(MediaSubscriptionService().check_subscription(subscription_id))
        self.assertEqual(checked["result"]["auto_submitted"], 0)
        self.assertEqual(source.resolve_titles, [source.items[0].title])
        backend.assert_called_once()
        admissions = self.admissions(subscription_id)
        self.assertEqual(len(admissions), 1)
        self.assertEqual(len(media_subscriptions_repo.list_active_media_download_admissions(subscription_id)), 1)
        request = download_requests_repo.get_download_request(admissions[0]["request_id"])
        assert request is not None
        self.assertEqual(request["qb_status"], "outcome_unknown")
        self.assertIn(request["status"], {"submitted", "manual_review"})
        candidate = media_subscriptions_repo.get_media_subscription_candidate(admissions[0]["candidate_id"])
        assert candidate is not None
        self.assertEqual(candidate["status"], "submitted")
        self.assert_check_persisted(subscription_id, candidates=2, submitted=0, missing=1)

    def test_exception_from_backend_remains_unknown_not_safe_failure(self):
        subscription_id, source, _indexer, backend = self.scenario(failures=())
        backend.side_effect = HttpInvalidResponse("backend raised after receiving input")
        checked = asyncio.run(MediaSubscriptionService().check_subscription(subscription_id))
        self.assertEqual(checked["result"]["auto_submitted"], 0)
        self.assertEqual(source.resolve_titles, [source.items[0].title])
        backend.assert_called_once()
        admission = self.admissions(subscription_id)[0]
        request = download_requests_repo.get_download_request(admission["request_id"])
        assert request is not None
        self.assertEqual(request["qb_status"], "outcome_unknown")
        self.assertIn(request["status"], {"submitted", "manual_review"})
        self.assertEqual(len(media_subscriptions_repo.list_active_media_download_admissions(subscription_id)), 1)

    def test_duplicate_bound_request_stops_candidate_search_without_backend_submit(self):
        subscription_id, source, _indexer, backend = self.scenario(failures=())
        existing = download_dispatcher.create_request(
            download_dispatcher.normalize_download_url(source.items[0].magnet),
            "", "", origin="indexer:nyaa",
        )
        request_id = int(existing["id"])
        download_requests_repo.update_download_request(request_id, targets="qb", status="submitted", qb_status="submitted")
        checked = asyncio.run(MediaSubscriptionService().check_subscription(subscription_id))
        self.assertEqual(checked["result"]["auto_submitted"], 1)
        self.assertEqual(source.resolve_titles, [source.items[0].title])
        backend.assert_not_called()
        admissions = self.admissions(subscription_id)
        self.assertEqual(len(admissions), 1)
        self.assertEqual(admissions[0]["request_id"], request_id)

    def test_failed_historical_request_creates_successor_before_marking_candidate_submitted(self):
        subscription_id, source, _indexer, backend = self.scenario(failures=())
        existing = download_dispatcher.create_request(
            download_dispatcher.normalize_download_url(source.items[0].magnet),
            "", "", origin="indexer:nyaa",
        )
        request_id = int(existing["id"])
        download_requests_repo.update_download_request(request_id, targets="qb", status="failed", qb_status="failed", error="历史下载失败")
        checked = asyncio.run(MediaSubscriptionService().check_subscription(subscription_id))
        backend.assert_called_once()
        self.assertEqual(checked["result"]["auto_submitted"], 1)
        candidates = media_subscriptions_repo.list_media_subscription_candidates(subscription_id, statuses=("available", "submitted"))
        submitted = [row for row in candidates if row["status"] == "submitted"]
        self.assertEqual(len(submitted), 1)
        successor = download_requests_repo.get_download_request(submitted[0]["request_id"])
        previous = download_requests_repo.get_download_request(request_id)
        assert successor is not None and previous is not None
        self.assertNotEqual(successor["id"], previous["id"])
        self.assertEqual((previous["status"], successor["status"]), ("failed", "submitted"))
        admissions = media_subscriptions_repo.list_active_media_download_admissions(subscription_id)
        self.assertEqual(len(admissions), 1)
        self.assertEqual(admissions[0]["request_id"], successor["id"])

    def test_lost_public_result_after_backend_acceptance_does_not_switch_source(self):
        subscription_id, source, _indexer, backend = self.scenario(failures=())
        # 故障注入在受理后的展示映射；使用真实创建 / 准入绑定 / 后端提交。
        with patch.object(downloads, "public_dispatch_summary", side_effect=HttpInvalidResponse("late failure")):
            admissions = self.assert_failure_stops_round(subscription_id, source, backend, submitted=True)
        self.assertEqual(len(admissions), 1)
        self.assertTrue(admissions[0]["request_id"])
        get_download_request_result = download_requests_repo.get_download_request(admissions[0]["request_id"])
        assert get_download_request_result is not None
        self.assertEqual(get_download_request_result["status"], "submitted")
        self.assertEqual(len(media_subscriptions_repo.list_active_media_download_admissions(subscription_id)), 1)

    def test_unclassified_unbound_creation_error_does_not_switch_source(self):
        subscription_id, source, _indexer, backend = self.scenario(failures=())
        # request_id=0 且 CAS 成功仍不充分：创建阶段的兜底错误没有提交前证明。
        with patch.object(downloads, "create_request", side_effect=RuntimeError("creation fault")):
            admissions = self.assert_failure_stops_round(subscription_id, source, backend, submitted=False)
        self.assertEqual([(row["status"], row["request_id"]) for row in admissions], [("failed", None)])

    def test_pre_submission_marker_cannot_release_a_concurrently_bound_request(self):
        subscription_id, source, _indexer, backend = self.scenario()
        bound_ids = []

        def bind_before_resolver_failure(item):
            admission = self.admissions(subscription_id)[0]
            created = download_dispatcher.create_request(
                download_dispatcher.normalize_download_url(item.magnet),
                "", "", origin="indexer:nyaa", admission_id=admission["id"],
            )
            bound_ids.append(int(created["id"]))

        source.before_resolve = bind_before_resolver_failure
        admissions = self.assert_failure_stops_round(subscription_id, source, backend, submitted=False)
        self.assertEqual(len(admissions), 1)
        self.assertEqual(admissions[0]["request_id"], bound_ids[0])
        self.assertEqual(admissions[0]["status"], "dispatching")
        self.assertEqual(len(media_subscriptions_repo.list_active_media_download_admissions(subscription_id)), 1)

    def test_pre_submission_marker_requires_successful_unbound_release_cas(self):
        subscription_id, source, _indexer, backend = self.scenario()

        def change_admission_before_failure(_item):
            admission = self.admissions(subscription_id)[0]
            media_subscriptions_repo.update_media_download_admission(admission["id"], status="completed", completed_at=db.now())

        source.before_resolve = change_admission_before_failure
        admissions = self.assert_failure_stops_round(subscription_id, source, backend, submitted=False)
        self.assertEqual([(row["status"], row["request_id"]) for row in admissions], [("completed", None)])

    def test_revision_changed_during_resolver_failure_cancels_instead_of_switching(self):
        subscription_id, source, _indexer, backend = self.scenario("tv", episode_count=2)
        source.before_resolve = lambda _item: media_subscriptions_repo.update_media_subscription_config(subscription_id, action="confirm", status="new")
        with self.assertRaises(MediaSubscriptionError) as raised:
            asyncio.run(MediaSubscriptionService().check_subscription(subscription_id))
        self.assertEqual(raised.exception.code, "cancelled")
        self.assertEqual(source.resolve_titles, [source.items[0].title])
        backend.assert_not_called()
        row = media_subscriptions_repo.get_media_subscription(subscription_id)
        assert row is not None
        self.assertEqual((row["action"], row["revision"], row["status"]), ("confirm", 2, "new"))
        self.assertEqual(media_subscriptions_repo.list_media_subscription_runs(subscription_id=subscription_id)[0]["status"], "cancelled")
        self.assertEqual(row["result_json"], "{}")

    def test_stop_event_during_resolver_failure_cancels_instead_of_switching(self):
        subscription_id, source, _indexer, backend = self.scenario("tv", episode_count=2)
        cancel_event = threading.Event()
        source.before_resolve = lambda _item: cancel_event.set()
        with self.assertRaises(MediaSubscriptionError) as raised:
            asyncio.run(MediaSubscriptionService().check_subscription(subscription_id, cancel_event=cancel_event))
        self.assertEqual(raised.exception.code, "cancelled")
        self.assertEqual(source.resolve_titles, [source.items[0].title])
        backend.assert_not_called()
        self.assertEqual(media_subscriptions_repo.list_media_subscription_runs(subscription_id=subscription_id)[0]["status"], "cancelled")
        self.assertEqual(media_subscriptions_repo.list_active_media_download_admissions(subscription_id), [])

    def test_async_cancellation_from_resolver_exits_without_switching(self):
        subscription_id, source, _indexer, backend = self.scenario("tv", episode_count=2)

        def cancel_resolver(_item):
            raise asyncio.CancelledError()

        source.before_resolve = cancel_resolver
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(MediaSubscriptionService().check_subscription(subscription_id))
        self.assertEqual(source.resolve_titles, [source.items[0].title])
        backend.assert_not_called()
        self.assertEqual(media_subscriptions_repo.list_media_subscription_runs(subscription_id=subscription_id)[0]["status"], "cancelled")
        self.assertEqual(media_subscriptions_repo.list_active_media_download_admissions(subscription_id), [])

    def test_failed_batch_persists_rotation_and_next_check_reaches_episode_thirteen(self):
        subscription_id, source, _indexer, backend = self.scenario(
            "tv", episode_count=13, failures=tuple(range(24)),
        )
        service = MediaSubscriptionService()
        first = asyncio.run(service.check_subscription(subscription_id))
        self.assertEqual(first["result"]["auto_submitted"], 0)
        self.assertEqual(len(source.resolve_titles), 24)
        backend.assert_not_called()
        payload = self.assert_check_persisted(subscription_id, candidates=24, submitted=0, missing=13)
        self.assertEqual(payload["_search_rotation"], {"revision": 1, "season": 1, "episode": 12})
        self.assertEqual(media_subscriptions_repo.list_active_media_download_admissions(subscription_id), [])
        self.assertEqual(self.request_count(), 0)
        source.resolve_titles.clear()
        second = asyncio.run(service.check_subscription(subscription_id))
        self.assertEqual(second["result"]["auto_submitted"], 1)
        self.assertEqual(source.resolve_titles[0], source.items[24].title)
        self.assertNotIn(source.items[25].title, source.resolve_titles)
        backend.assert_called_once()
        payload = self.assert_check_persisted(subscription_id, candidates=24, submitted=1, missing=13)
        self.assertEqual(payload["_search_rotation"], {"revision": 1, "season": 1, "episode": 11})
        self.assertEqual(len(media_subscriptions_repo.list_media_subscription_runs(subscription_id=subscription_id)), 2)

    def test_nonpublic_entry_preserves_resolver_exception_type(self):
        _subscription_id, source, indexer, backend = self.scenario()
        result_id = indexer.result_store.put(replace(source.items[0], result_id=None))
        with self.assertRaises(HttpInvalidResponse):
            asyncio.run(downloads.download_indexer_result(indexer, result_id, "qb"))
        result = asyncio.run(downloads.download_indexer_result_public(indexer, result_id, "qb"))
        self.assertTrue(result["pre_submission_failure"])
        self.assertEqual((result["ok"], result["request_id"], result["duplicate"]), (False, 0, False))
        self.assertNotIn("private upstream", result["error"])
        backend.assert_not_called()

    def test_pause_during_resolver_failure_cancels_round_without_switching(self):
        subscription_id, source, _indexer, backend = self.scenario()
        source.before_resolve = lambda _item: media_subscriptions_repo.update_media_subscription_config(
            subscription_id, enabled=0, status="paused",
        )
        with self.assertRaises(MediaSubscriptionError) as raised:
            asyncio.run(MediaSubscriptionService().check_subscription(subscription_id))
        self.assertEqual(raised.exception.code, "cancelled")
        self.assertEqual(source.resolve_titles, [source.items[0].title])
        backend.assert_not_called()
        get_media_subscription_result_2 = media_subscriptions_repo.get_media_subscription(subscription_id)
        assert get_media_subscription_result_2 is not None
        self.assertEqual(get_media_subscription_result_2["status"], "paused")
        self.assertEqual(media_subscriptions_repo.list_media_subscription_runs(subscription_id=subscription_id)[0]["status"], "cancelled")

    def test_delete_during_resolver_failure_cancels_round_without_switching(self):
        subscription_id, source, _indexer, backend = self.scenario()
        source.before_resolve = lambda _item: media_subscriptions_repo.delete_media_subscription(subscription_id)
        with self.assertRaises(MediaSubscriptionError) as raised:
            asyncio.run(MediaSubscriptionService().check_subscription(subscription_id))
        self.assertEqual(raised.exception.code, "cancelled")
        self.assertEqual(source.resolve_titles, [source.items[0].title])
        backend.assert_not_called()
        self.assertIsNone(media_subscriptions_repo.get_media_subscription(subscription_id))
        self.assertEqual(media_subscriptions_repo.list_media_subscription_runs(subscription_id=subscription_id)[0]["status"], "cancelled")

    def test_manual_candidate_keeps_error_contract_and_can_be_explicitly_retried(self):
        subscription_id, source, indexer, backend = self.scenario()
        result_id = indexer.result_store.put(replace(source.items[0], result_id=None))
        candidate_id = media_subscriptions_repo.replace_media_subscription_candidates(
            subscription_id, "tmdb:86034:movie", season=None, episode=None,
            candidates=[{**source.items[0].to_public_dict(), "result_id": result_id}],
            expires_at="2099-01-01 00:00:00",
        )[0]
        service = MediaSubscriptionService()
        with self.assertRaises(MediaSubscriptionError) as raised:
            asyncio.run(service.download_candidate(candidate_id))
        self.assertEqual((raised.exception.code, raised.exception.status_code), ("download_failed", 502))
        self.assertTrue(raised.exception.can_try_next_candidate)
        get_media_subscription_candidate_result = media_subscriptions_repo.get_media_subscription_candidate(candidate_id)
        assert get_media_subscription_candidate_result is not None
        self.assertEqual(get_media_subscription_candidate_result["status"], "available")
        backend.assert_not_called()
        source.fail_titles.clear()
        result = asyncio.run(service.download_candidate(candidate_id))
        self.assertTrue(result["ok"])
        backend.assert_called_once()
        self.assertEqual(len(self.admissions(subscription_id)), 2)

    def test_nonpublic_entry_preserves_invalid_download_data_exception(self):
        _subscription_id, source, indexer, backend = self.scenario(failures=())
        source.resolved_by_title[source.items[0].title] = ResolvedDownload("torrent", b"not-a-torrent")
        result_id = indexer.result_store.put(replace(source.items[0], result_id=None))
        with self.assertRaises(downloads.InvalidDownloadData):
            asyncio.run(downloads.download_indexer_result(indexer, result_id, "qb"))
        result = asyncio.run(downloads.download_indexer_result_public(indexer, result_id, "qb"))
        self.assertEqual((result["pre_submission_failure"], result["error"]), (True, "资源下载数据无效"))
        backend.assert_not_called()

    def test_explicit_request_creation_failure_is_not_resolver_failure(self):
        subscription_id, source, _indexer, backend = self.scenario(failures=())
        with patch.object(downloads, "create_request", return_value={"id": 0, "created": False}):
            admissions = self.assert_failure_stops_round(subscription_id, source, backend, submitted=False)
        self.assertEqual([(row["status"], row["request_id"], row["error"]) for row in admissions],
                         [("failed", None, "下载请求创建失败")])
