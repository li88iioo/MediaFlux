"""候选发布与配置/检查归属变更必须在同一写事务中串行。"""
from __future__ import annotations

import asyncio
import threading
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.indexers.models import AggregatedIndexerResult, IndexerItem
from app.modules.media_identity import build_media_key
from app.modules.media_subscriptions import MediaSubscriptionError, MediaSubscriptionService, _ExpectedMedia
from app.repositories import media_subscriptions as repo
from tests.support import isolated_test_database, seed_media_subscription_candidates
from tests.test_media_subscriptions import check_resource_candidates


async def _direct(awaitable):
    return await awaitable


@pytest.mark.parametrize("media_type", ["tv", "movie"])
@pytest.mark.parametrize("mutation", ["configuration", "pause", "delete", "replacement_run"])
def test_late_candidate_publication_cannot_cross_check_ownership(media_type, mutation):
    with isolated_test_database(), ExitStack() as stack:
        sid = repo.add_media_subscription(
            provider="tmdb", external_id="86034", tmdb_id="86034", media_type=media_type,
            title="Publication Fixture", original_title="Publication Fixture", year="2020",
            action="confirm", download_target="qb", sites=["nyaa"],
        )
        tv = media_type == "tv"
        key = build_media_key("86034", media_type, 1, 1) if tv else build_media_key("86034", media_type)
        target = _ExpectedMedia(key, season=1 if tv else None, episode=1 if tv else None)
        item = IndexerItem(
            site_id="nyaa", site_name="Offline Fixture",
            title="Publication Fixture S01E01 1080p" if tv else "Publication Fixture 2020 1080p",
            detail_url="https://fixture.invalid/1", download_state="ready",
            relevance_score=99,
            match_reasons=("title_exact",),
            download_kinds=("magnet",), magnet="magnet:?xt=urn:btih:" + "1" * 40,
        ).with_result_id("old-run-resource")
        indexer = SimpleNamespace(search_media=AsyncMock(return_value=AggregatedIndexerResult(
            query="Publication Fixture", page=1, items=[item],
            sites_attempted=("nyaa",), sites_succeeded=("nyaa",),
        )))
        service = MediaSubscriptionService()
        stack.enter_context(patch("app.modules.media_subscriptions.config.get_bool", return_value=True))
        stack.enter_context(patch("app.modules.media_subscriptions._tmdb_detail", return_value={
            "name": "Publication Fixture", "title": "Publication Fixture", "release_date": "2020-01-01",
        }))
        stack.enter_context(patch.object(service, "_expected_tv", AsyncMock(return_value=([target], 0, 0))))
        stack.enter_context(patch("app.modules.media_subscriptions.inspect_series_episode_sources", return_value=[
            {"status": "ready", "server_type": "jellyfin", "episodes": []},
        ]))
        stack.enter_context(patch("app.modules.media_subscriptions.inspect_media_identity_sources", return_value=[
            {"status": "ready", "server_type": "jellyfin", "present": False},
        ]))
        stack.enter_context(patch("app.modules.media_subscriptions.get_indexer_service", return_value=indexer))
        stack.enter_context(patch("app.modules.media_subscriptions.run_indexer_awaitable", new=_direct))
        notify = stack.enter_context(patch("app.modules.media_subscription_notifications.drain_media_subscription_notifications"))
        before_write, resume = threading.Event(), threading.Event()
        publish = repo.replace_media_subscription_candidates
        outcomes: list[Any] = []

        def blocked_publish(*args, **kwargs):
            before_write.set()
            assert resume.wait(10), "候选写入未释放"
            return publish(*args, **kwargs)

        def old_check():
            try:
                outcomes.append(asyncio.run(service.check_subscription(sid)))
            except BaseException as exc:
                outcomes.append(exc)

        stack.enter_context(patch.object(repo, "replace_media_subscription_candidates", side_effect=blocked_publish))
        worker = threading.Thread(target=old_check)
        worker.start()
        new_run = None
        try:
            assert before_write.wait(10), "旧检查未进入真实候选发布边界"
            old_run = repo.list_media_subscription_runs(subscription_id=sid)[0]
            if mutation == "replacement_run":
                repo.cancel_media_subscription_run(
                    old_run["id"], subscription_id=sid, subscription_revision=1,
                    reason="前一检查被取消",
                )
            elif mutation == "delete":
                service.delete_subscription(sid)
            else:
                service.update_subscription(sid, {"enabled": False} if mutation == "pause" else {"check_interval_minutes": 30})
            if mutation in {"configuration", "replacement_run"}:
                new_run = repo.claim_media_subscription_check_run(sid)
                assert new_run is not None
                current = repo.get_media_subscription(sid)
                assert current is not None
                publish(
                    sid, key, season=target.season, episode=target.episode,
                    revision=current["revision"], run_id=new_run,
                    candidates=[{"result_id": "new-run-resource", "title": "Current candidate"}],
                    expires_at="2099-01-01 00:00:00",
                )
        finally:
            resume.set()
            worker.join(timeout=10)
        assert not worker.is_alive()
        assert len(outcomes) == 1
        error = outcomes[0]
        assert isinstance(error, MediaSubscriptionError)
        assert (error.code, error.status_code) == ("cancelled", 409)
        candidates = repo.list_media_subscription_candidates(sid, statuses=())
        assert not any(row["result_id"] == "old-run-resource" for row in candidates)
        if new_run is not None:
            assert [(row["result_id"], row["status"]) for row in candidates] == [("new-run-resource", "available")]
            current = repo.get_media_subscription(sid)
            assert current is not None and current["status"] == "checking"
            assert repo.list_media_subscription_runs(subscription_id=sid)[0]["status"] == "running"
        assert repo.list_active_media_download_admissions(sid) == []
        notify.assert_not_called()


def test_publication_returns_each_input_in_order_without_reviving_terminal_candidates():
    with isolated_test_database():
        sid = repo.add_media_subscription(
            provider="tmdb", external_id="1", tmdb_id="1", media_type="movie", title="Fixture",
        )
        run_id = repo.claim_media_subscription_check_run(sid)
        assert run_id is not None
        options: dict[str, Any] = dict(revision=1, run_id=run_id, season=None, episode=None, expires_at="2099-01-01 00:00:00")
        first = repo.replace_media_subscription_candidates(
            sid, "tmdb:1:movie", candidates=[{"result_id": name} for name in ("a", "b", "c")], **options,
        )
        repo.update_media_subscription_candidate(first[0]["id"], status="submitted")
        repo.update_media_subscription_candidate(first[1]["id"], status="dismissed")
        repeated = repo.replace_media_subscription_candidates(
            sid, "tmdb:1:movie", candidates=[{"result_id": name} for name in ("c", "", "a", "c", None, "b")], **options,
        )
        assert repeated[1]["id"] == repeated[4]["id"]
        assert [(row["id"], row["result_id"], row["status"]) for row in repeated] == [
            (first[2]["id"], "c", "available"), (repeated[1]["id"], "", "available"),
            (first[0]["id"], "a", "submitted"), (first[2]["id"], "c", "available"),
            (repeated[1]["id"], "", "available"), (first[1]["id"], "b", "dismissed"),
        ]


@pytest.mark.parametrize("media_type", ["tv", "movie"])
@pytest.mark.parametrize("failed", [False, True], ids=["empty", "failure"])
def test_search_failure_preserves_candidates_but_successful_empty_search_expires_them(media_type, failed):
    with isolated_test_database():
        sid = repo.add_media_subscription(
            provider="tmdb", external_id="1", tmdb_id="1", media_type=media_type, title="Fixture",
        )
        target = _ExpectedMedia(build_media_key("1", media_type, 1, 1), 1, 1) if media_type == "tv" else _ExpectedMedia(build_media_key("1", media_type))
        cid = seed_media_subscription_candidates(
            sid, target.media_key, season=target.season, episode=target.episode,
            candidates=[{"result_id": "previous-resource"}], expires_at="2099-01-01 00:00:00",
        )[0]
        indexer = SimpleNamespace(search_media=AsyncMock(
            side_effect=RuntimeError("offline fixture") if failed else None,
            return_value=AggregatedIndexerResult(
                query="Fixture", page=1, items=[], sites_attempted=("nyaa",), sites_succeeded=("nyaa",),
            ),
        ))
        with patch("app.modules.media_subscriptions.config.get_bool", return_value=True), patch(
            "app.modules.media_subscriptions.get_indexer_service", return_value=indexer,
        ), patch("app.modules.media_subscriptions.run_indexer_awaitable", new=_direct):
            result = asyncio.run(check_resource_candidates(
                MediaSubscriptionService(), repo.get_media_subscription(sid), {}, [target],
            ))
        candidate = repo.get_media_subscription_candidate(cid)
        assert candidate is not None
        assert candidate["status"] == ("available" if failed else "expired")
        assert (result["candidate_count"], result["auto_submitted"]) == (0, 0)


def test_tv_processes_search_publish_and_auto_download_before_the_next_target():
    with isolated_test_database():
        sid = repo.add_media_subscription(
            provider="tmdb", external_id="1", tmdb_id="1", media_type="tv", title="Fixture", action="auto",
        )
        targets = [_ExpectedMedia(build_media_key("1", "tv", 1, ep), 1, ep) for ep in (1, 2)]
        events = []
        async def search(request, _sites):
            events.append(("search", request.episode))
            item = IndexerItem(
                site_id="nyaa", site_name="Fixture", title=f"Fixture S01E{request.episode:02d} 1080p",
                result_id=f"resource-episode-{request.episode}", download_state="ready", download_kinds=("magnet",),
                relevance_score=99, match_reasons=("title_exact",),
            )
            return AggregatedIndexerResult(
                query="Fixture", page=1, items=[item], sites_attempted=("nyaa",), sites_succeeded=("nyaa",),
            )
        async def download(cid, *_args, **_kwargs):
            candidate = repo.get_media_subscription_candidate(cid)
            assert candidate is not None and candidate["status"] == "available"
            events.append(("download", candidate["episode"]))
            return {"ok": True}
        service = MediaSubscriptionService()
        with patch("app.modules.media_subscriptions.config.get_bool", return_value=True), patch(
            "app.modules.media_subscriptions.get_indexer_service", return_value=SimpleNamespace(search_media=search),
        ), patch("app.modules.media_subscriptions.run_indexer_awaitable", new=_direct), patch.object(
            service, "download_candidate", new=download,
        ):
            result = asyncio.run(check_resource_candidates(service, repo.get_media_subscription(sid), {}, targets))
        assert events == [("search", 1), ("download", 1), ("search", 2), ("download", 2)]
        assert (result["candidate_count"], result["auto_submitted"]) == (2, 2)
