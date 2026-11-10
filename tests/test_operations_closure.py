"""运维配置发布、消费者切换与真实关停结果的离线契约。"""

from typing import cast
from starlette.requests import Request
import os
import threading
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import config
from app.modules import local_media_scheduler as scheduler_module
from app.modules import local_media_service as local
from app.modules.scraper import TMDBScraper
from app.routes import api
from tests.support import initialized_web_runtime

OLD = {
    "TMDB_API_KEY": "fixture-old-key", "TMDB_API_URL": "http://127.0.0.1:43101/3",
    "TMDB_MATCH_MODE": "strict", "PROXY_URL": "http://127.0.0.1:43201",
}
NEW = {
    "TMDB_API_KEY": "fixture-new-key", "TMDB_API_URL": "http://127.0.0.1:43102/3",
    "TMDB_MATCH_MODE": "loose", "PROXY_URL": "http://127.0.0.1:43202",
}


@pytest.fixture
def services():
    with initialized_web_runtime(), ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ))
        for key in OLD:
            os.environ.pop(key, None)
        stack.enter_context(patch.object(
            config, "_STARTUP_ENV_OVERRIDES", config._STARTUP_ENV_OVERRIDES.difference(OLD),
        ))
        config.set_and_save(OLD)
        web = local.LocalMediaService()
        scheduler = scheduler_module.LocalMediaScheduler()
        stack.enter_context(patch.object(local, "_service", web))
        stack.enter_context(patch.object(scheduler_module, "_scheduler", scheduler))
        stack.enter_context(patch.object(api, "require_api_login"))
        stack.enter_context(patch("app.modules.login_wallpaper.schedule_login_wallpaper_refresh"))
        try:
            yield web, scheduler
        finally:
            assert scheduler.shutdown(timeout=2.0)
            assert web.close()


def save(values):
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(background_services_enabled=False)))
    return api.save_config(cast(Request, request), values)


def runtime(service):
    return {
        "TMDB_API_KEY": service.scraper.client.api_key,
        "TMDB_API_URL": service.scraper.client.base_url,
        "TMDB_MATCH_MODE": service.scraper.match_mode,
        "PROXY_URL": service.scraper.client.session.proxies.get("https", ""),
    }


def test_save_refreshes_both_idle_consumers_without_losing_inspections(services):
    web, scheduler = services
    old_scrapers = [web.scraper, scheduler.service.scraper]
    inspections = web.inspections
    assert save(NEW) == {"success": True}
    assert config.get_many(tuple(NEW)) == NEW
    assert runtime(web) == runtime(scheduler.service) == NEW
    assert web.inspections is inspections
    assert local.get_local_media_service() is web
    assert scheduler_module.get_local_media_scheduler() is scheduler
    assert all(scraper._closed for scraper in old_scrapers)


@pytest.mark.parametrize("consumer", ["web", "scheduler"])
def test_save_warns_and_never_closes_a_client_in_flight(services, consumer):
    web, scheduler = services
    service = web if consumer == "web" else scheduler.service
    old = service.scraper
    entered, release = threading.Event(), threading.Event()
    errors = []

    def search(*args):
        entered.set()
        assert release.wait(3)
        assert not old._closed
        return []

    def run():
        try:
            service.search("fixture")
        except BaseException as exc:  # noqa: BLE001 - 将故障线程异常回传主线程断言，不允许漏报。
            errors.append(exc)

    with patch.object(old, "search_candidates", side_effect=search):
        thread = threading.Thread(target=run)
        thread.start()
        try:
            assert entered.wait(2)
            result = save(NEW)
            assert isinstance(result, dict)
            assert result["success"] is True and result["warnings"]
            # 相同值再保存仍须如实报告 pending，不能从 changed=0 路径假成功。
            repeat_response = save(NEW)
            assert isinstance(repeat_response, dict)
            assert repeat_response["warnings"]
            assert runtime(service) == OLD
            assert not old._closed
            with pytest.raises(local.LocalMediaServiceError, match="切换配置"):
                service.search("new operation")
        finally:
            release.set()
            thread.join(3)
    assert not thread.is_alive() and not errors
    with patch.object(TMDBScraper, "search_candidates", return_value=[]):
        assert service.search("after old operation") == []
    assert runtime(service) == NEW and old._closed


def test_new_operation_reloads_config_even_without_api_callback(services):
    web, scheduler = services
    config.set_and_save(NEW)
    with patch.object(TMDBScraper, "search_candidates", return_value=[]):
        for service in (web, scheduler.service):
            service.search("fixture")
            assert runtime(service) == NEW


def test_failed_resource_close_is_reported_and_retried_before_new_operation(services):
    web, _scheduler = services
    old = web.scraper
    with patch.object(old, "close", side_effect=[False, True]):
        result = save(NEW)
        assert isinstance(result, dict)
        assert result["warnings"]
        assert web.scraper is old
        with patch.object(TMDBScraper, "search_candidates", return_value=[]):
            web.search("retry")
        assert runtime(web) == NEW and web.scraper is not old
    # mock 的第二次成功不负责真实释放 Session。
    assert old.close()


def test_constructor_failure_warns_and_configuration_revert_does_not_reuse_closed_client(services):
    web, _scheduler = services
    old = web.scraper
    with patch.object(local, "TMDBScraper", side_effect=RuntimeError("fixture constructor failure")):
        result = save(NEW)
    assert isinstance(result, dict)
    assert result["warnings"] and result["restart_required"] is True
    assert old._closed
    config.set_and_save(OLD)
    with patch.object(TMDBScraper, "search_candidates", return_value=[]):
        web.search("retry previous settings")
    assert runtime(web) == OLD and web.scraper is not old


def test_parallel_planning_pins_the_shared_parent_client(services):
    _web, scheduler = services
    old = scheduler.service.scraper
    entered, release = threading.Event(), threading.Event()
    errors = []
    planners = []
    create = scheduler.service.create_planning_worker

    def create_planner():
        planner = create()
        planners.append(planner)

        def prepare(*args):
            entered.set()
            assert release.wait(3)
            assert planner.scraper is old and not old._closed

        planner.prepare_task = prepare
        return planner

    def run():
        try:
            assert scheduler._run_parallel_planning_batch([SimpleNamespace(id=1)], 2) == 1
        except BaseException as exc:  # noqa: BLE001 - 将故障线程异常回传主线程断言，不允许漏报。
            errors.append(exc)

    with (
        patch.object(scheduler.service, "create_planning_worker", side_effect=create_planner),
        patch.object(scheduler, "_claim_waiting_task", return_value="fixture-claim"),
        patch.object(scheduler, "_execute_claimed_task", return_value=True),
    ):
        thread = threading.Thread(target=run)
        thread.start()
        try:
            assert entered.wait(2)
            repeat_response = save(NEW)
            assert isinstance(repeat_response, dict)
            assert repeat_response["warnings"]
            assert not old._closed
            assert scheduler.service._active_operations == 1
        finally:
            release.set()
            thread.join(3)
    assert not errors and not thread.is_alive()
    assert planners and all(planner._closed for planner in planners)
    with patch.object(TMDBScraper, "search_candidates", return_value=[]):
        scheduler.service.search("next task")
    assert runtime(scheduler.service) == NEW and old._closed


def test_injected_scraper_is_not_reconfigured_or_closed(services):
    web, _scheduler = services
    injected = local.LocalMediaService(scraper=web.scraper)
    try:
        config.set_and_save(NEW)
        assert injected.reload_config()
        assert injected.scraper is web.scraper and not web.scraper._closed
    finally:
        assert injected.close()
    assert not web.scraper._closed


@pytest.fixture
def bot_stopped():
    from app.bot import handlers
    from app.bot.agent_adapter import AGENT_EXECUTOR
    with ExitStack() as stack:
        for name in ("_bot", "_bot_thread", "_bot_thread_stop", "_progress_recovery_thread", "_progress_recovery_stop"):
            stack.enter_context(patch.object(handlers, name, None))
        agent_stop = stack.enter_context(patch.object(AGENT_EXECUTOR, "stop", return_value=True))
        yield handlers, agent_stop


@pytest.mark.parametrize("outcome", [True, False, RuntimeError("fixture close failure")])
def test_bot_stop_includes_indexer_outcome_without_changing_agent_cancellation(bot_stopped, outcome):
    handlers, agent_stop = bot_stopped
    replacement = patch("app.modules.telegram_resource_search.shutdown_telegram_indexer_worker", side_effect=outcome) if isinstance(outcome, Exception) else patch("app.modules.telegram_resource_search.shutdown_telegram_indexer_worker", return_value=outcome)
    with replacement:
        assert handlers.stop_bot(timeout=0.1) is (outcome is True)
    agent_stop.assert_called_once_with(timeout=0.1, cancel_queries=True)


@pytest.mark.parametrize("finished", [False, True])
def test_bot_restart_requires_indexer_shutdown_and_preserves_cancel_false(bot_stopped, finished):
    handlers, agent_stop = bot_stopped
    with (
        patch("app.modules.telegram_resource_search.shutdown_telegram_indexer_worker", return_value=finished),
        patch.object(handlers, "_start_bot_locked", return_value=True) as start,
        patch("app.notifier.reset") as reset,
    ):
        assert handlers.restart_bot() is finished
    agent_stop.assert_called_once_with(timeout=5.0, cancel_queries=False)
    assert start.call_count == reset.call_count == int(finished)


def test_bot_stop_rejects_real_worker_until_retry_finishes(bot_stopped):
    from app.modules import telegram_resource_search as tg
    handlers, _agent_stop = bot_stopped
    worker = tg.TelegramIndexerWorker()

    class Service:
        def __init__(self):
            self.calls = 0

        async def aclose(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("fixture first close failure")

    with patch.object(tg, "build_indexer_service", return_value=Service()):
        worker._ensure_started()
        try:
            with patch.object(tg, "shutdown_telegram_indexer_worker", side_effect=worker.stop):
                assert handlers.stop_bot(timeout=0.05) is False
                assert worker._thread is not None
                assert worker._thread.is_alive() and worker._stopping
                assert handlers.stop_bot(timeout=1.0) is True
        finally:
            assert worker.stop(timeout=1.0)


def test_reload_waiting_for_close_never_holds_config_lock(services):
    web, _scheduler = services
    config.set_and_save(NEW)
    closing, release_close, reload_waiting = threading.Event(), threading.Event(), threading.Event()
    real_lock = threading.Lock()
    close = web.organizer.close
    results = {}

    class ObservedCloseLock:
        def __enter__(self):
            if threading.current_thread().name == "fixture-reloader":
                reload_waiting.set()
            real_lock.acquire()
            return self

        def __exit__(self, *args):
            real_lock.release()

    def pause_close():
        closing.set()
        assert release_close.wait(3)
        return close()

    with patch.object(web, "_close_call_lock", ObservedCloseLock()), patch.object(web.organizer, "close", side_effect=pause_close):
        closer = threading.Thread(target=lambda: results.update(close=web.close()))
        reloader = threading.Thread(name="fixture-reloader", target=lambda: results.update(reload=web.reload_config()))
        closer.start()
        try:
            assert closing.wait(2)
            reloader.start()
            assert reload_waiting.wait(2)
            # 若 reload 在等 close 时持有 config 锁，close 内任何配置读取都会互锁。
            acquired = config._lock.acquire(timeout=0.5)
            if acquired:
                config._lock.release()
            assert acquired
        finally:
            release_close.set()
            closer.join(3)
            if reloader.ident is not None:
                reloader.join(3)
    assert not closer.is_alive() and not reloader.is_alive()
    assert results == {"close": True, "reload": False}
