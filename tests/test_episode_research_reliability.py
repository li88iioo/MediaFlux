"""季集研究四组可靠性回归：身份、标准顺序、总期限、关闭所有权。

复用合成证据 fixture 和 scripted model；配置/cache/额度均为进程内 fake。
真实 reader、TMDBClient 生命周期和 Kernel 可执行，但绝不访问真实网络或凭据。
"""
from __future__ import annotations

import asyncio
import copy
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.repositories import agent_web_search as agent_web_search_repo
from app.clients.tmdb import TMDBClient
from app.concurrency import KeyedSingleFlight
from app.modules import agent_episode_research as runner
from app.modules import episode_research as core
from app.modules import episode_research_service as service
from app.repositories import episode_research as repository
from tests.test_agent_episode_research import ScriptedModel, call, rounds
from tests.test_episode_research_validation import (
    GROUP_ID,
    EvidenceClient,
    case_payload,
)


class _OfflineCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.payload = case_payload()
        self.payload["kind"] = "guangya"
        self.cache = {}
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("network forbidden")))
        self.enterContext(patch("requests.sessions.Session.request", side_effect=AssertionError("HTTP forbidden")))
        self.enterContext(patch.object(core, "TMDBClient", side_effect=AssertionError("real provider construction forbidden")))
        self.enterContext(patch.object(runner.ProviderSettings, "from_config", side_effect=AssertionError("credentials forbidden")))
        self.enterContext(patch.object(runner, "OpenAICompatibleModelAdapter", side_effect=AssertionError("real LLM forbidden")))
        self.enterContext(patch.object(runner, "search_web", side_effect=AssertionError("real search forbidden")))
        self.enterContext(patch.object(runner, "read_web", side_effect=AssertionError("real web read forbidden")))
        self.enterContext(patch.object(service, "episode_research_enabled", return_value=True))
        self.enterContext(patch.object(service, "ensure_sync_bridge_available", return_value=None))
        self.enterContext(patch.object(service.config, "get", return_value="10"))
        self.enterContext(patch.object(service, "_flights", KeyedSingleFlight(max_entries=64)))
        self.enterContext(patch.object(service, "_running", threading.BoundedSemaphore(2)))
        self.enterContext(patch.object(repository, "get_episode_research_cache", side_effect=self._cache_get))
        self.enterContext(patch.object(repository, "put_episode_research_cache", side_effect=self._cache_put))
        self.enterContext(patch.object(repository, "invalidate_episode_research_cache", side_effect=lambda key: self.cache.pop(key, None)))
        self.charge = self.enterContext(patch.object(agent_web_search_repo, "reserve_agent_web_search_credits", return_value=True))
        self.enterContext(patch.object(agent_web_search_repo, "current_agent_web_search_usage_date", return_value="2026-09-11"))

    def _cache_get(self, key, **_kwargs):
        return copy.deepcopy(self.cache.get(key))

    def _cache_put(self, key, payload, *, status, **_kwargs):
        self.cache[key] = {"status": status, "payload": copy.deepcopy(payload)}

    def reader(self, payload=None, client=None):
        case = core.normalize_case(self.payload if payload is None else payload)
        reader = core.EpisodeEvidenceReader(case, client=client or EvidenceClient())
        self.addCleanup(reader.close)
        return reader

    @staticmethod
    def read_evidence(reader):
        reader.inspect_candidate(0)
        reader.list_groups(0)
        reader.read_group(0, GROUP_ID)

    def prove(self, payload=None, client=None):
        reader = self.reader(payload, client)
        self.read_evidence(reader)
        return reader.validate(0, GROUP_ID)

    def cached_proposal(self):
        proposal = self.prove()
        self._cache_put(proposal["case_key"], {"proposal": proposal}, status="verified")
        return proposal

    @staticmethod
    def real_client(session):
        # 显式提供所有可能读取配置的构造参数；session为本地替身，不发HTTP。
        return TMDBClient(api_key="offline-fixture-key", base_url="https://tmdb.fixture.invalid/3",
                          proxy_url="", timeout=10, retries=0, session=session)


class EpisodeResearchIdentityReliabilityTests(_OfflineCase):
    def title_payload(self, title):
        value = copy.deepcopy(self.payload)
        for number, item in enumerate(value["files"], 11):
            item["name"] = f"{title}.S01E{number:02d}.NF.WEB-DL.mkv"
        return value

    def test_short_and_explicit_numeric_work_titles_are_preserved(self):
        for title in ("诛仙", "V", "24", "1899"):
            with self.subTest(title=title):
                case = core.normalize_case(self.title_payload(title))
                self.assertEqual([row["source_title"] for row in case["files"]], [title] * 4)

    def test_short_or_numeric_wrong_work_cannot_borrow_directory_identity(self):
        for title in ("诛仙", "V", "24", "1899"):
            with self.subTest(title=title):
                reader = self.reader(self.title_payload(title))
                with self.assertRaises(core.EpisodeResearchError) as raised:
                    reader.inspect_candidate(0)
                self.assertEqual(raised.exception.code, "source_identity_unproven")

    def test_matching_short_or_numeric_official_title_can_still_verify(self):
        for title in ("诛仙", "V", "24", "1899"):
            with self.subTest(title=title):
                value = self.title_payload(title)
                value["identity"] = title
                value["directory"] = "/private/" + title
                value["candidates"][0]["title"] = title
                client = EvidenceClient()
                client.data["/tv/100"].update(name=title, original_name=title)
                proposal = self.prove(value, client)
                self.assertEqual(proposal["status"], "verified")
                self.assertEqual([row["episode_id"] for row in proposal["mappings"]], [1011, 1012, 1013, 1014])

    def test_one_short_conflicting_file_cannot_hide_in_an_otherwise_matching_pack(self):
        value = copy.deepcopy(self.payload)
        value["files"][-1]["name"] = "诛仙.S01E14.NF.WEB-DL.mkv"
        reader = self.reader(value)
        with self.assertRaises(core.EpisodeResearchError) as raised:
            reader.inspect_candidate(0)
        self.assertEqual(raised.exception.code, "source_identity_unproven")

    def test_episode_only_file_names_still_use_proven_directory_context(self):
        value = copy.deepcopy(self.payload)
        for number, item in enumerate(value["files"], 11):
            item["name"] = f"S01E{number:02d}.mkv"
        case = core.normalize_case(value)
        self.assertEqual([row["source_title"] for row in case["files"]], [""] * 4)
        self.assertEqual(self.prove(value)["status"], "verified")


class EpisodeResearchStandardOrderReliabilityTests(_OfflineCase):
    def native_fixture(self, order=(1, 2, 0), *, target_season=1):
        value = copy.deepcopy(self.payload)
        value["files"] = [{"name": f"Fixture.Show.S01E{number:02d}.WEB-DL.mkv", "size": 1024,
                           "season": 1, "episode": number} for number in range(1, 4)]
        client = EvidenceClient()
        standard = [{"id": 1000+number, "season_number": 1, "episode_number": number,
                     "name": f"Episode {number}"} for number in range(1, 4)]
        target = standard if target_season == 1 else [
            {"id": 1010+number, "season_number": 0, "episode_number": number,
             "name": f"Special {number}"} for number in range(1, 4)]
        client.data["/tv/100"]["seasons"] = [
            {"season_number": 0, "episode_count": 3}, {"season_number": 1, "episode_count": 3}]
        client.data["/tv/100/season/1"] = {"season_number": 1, "episodes": standard}
        client.data[f"/tv/100/season/{target_season}"] = {"season_number": target_season, "episodes": target}
        client.data["/tv/100/episode_groups"]["results"] = [
            {"id": GROUP_ID, "name": "DVD Alternative Order", "group_count": 1, "episode_count": 3}]
        client.data[f"/tv/episode_group/{GROUP_ID}"] = {
            "id": GROUP_ID, "name": "DVD Alternative Order", "groups": [{"order": 1, "episodes": [
                dict(target[position], order=index) for index, position in enumerate(order)]}]}
        return value, client

    def test_standard_order_is_a_competitor_even_when_not_listed_as_episode_group(self):
        value, client = self.native_fixture()
        reader = self.reader(value, client)
        self.read_evidence(reader)
        with self.assertRaises(core.EpisodeResearchError) as raised:
            reader.validate(0, GROUP_ID)
        self.assertEqual(raised.exception.code, "standard_order_conflict")
        self.assertIn("/tv/100/season/1", client.requests)
        self.assertEqual(len(client.data["/tv/100/episode_groups"]["results"]), 1)

    def test_valid_standard_source_cannot_be_reassigned_to_different_season_ids(self):
        value, client = self.native_fixture(order=(0, 1, 2), target_season=0)
        reader = self.reader(value, client)
        self.read_evidence(reader)
        with self.assertRaises(core.EpisodeResearchError) as raised:
            reader.validate(0, GROUP_ID)
        self.assertEqual(raised.exception.code, "standard_order_conflict")
        self.assertIn("/tv/100/season/1", client.requests)

    def test_group_agreeing_with_standard_order_and_stable_ids_is_not_rejected(self):
        value, client = self.native_fixture(order=(0, 1, 2))
        proposal = self.prove(value, client)
        self.assertEqual(proposal["status"], "verified")
        self.assertEqual([(row["source_episode"], row["target_episode"], row["episode_id"])
                          for row in proposal["mappings"]], [(1, 1, 1001), (2, 2, 1002), (3, 3, 1003)])

    def test_complete_tail_still_verifies_when_standard_source_episodes_do_not_exist(self):
        client = EvidenceClient()
        proposal = self.prove(client=client)
        self.assertEqual(proposal["status"], "verified")
        self.assertIn("/tv/100/season/1", client.requests)
        self.assertEqual([(row["source_episode"], row["target_season"], row["episode_id"])
                          for row in proposal["mappings"]], [(n, 0, 1000+n) for n in range(11, 15)])


class _BlockedSession:
    """执行真实TMDBClient的active/close逻辑，只把socket替换为Event等待。"""
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.active = False
        self.close_count = 0
        self.closed_during_get = False
        self.get_count = 0

    def get(self, url, **_kwargs):
        if url != "https://tmdb.fixture.invalid/3/tv/100":
            raise AssertionError("unexpected fixture URL")
        self.get_count += 1
        self.active = True
        self.entered.set()
        try:
            if not self.release.wait(3):
                raise AssertionError("blocked HTTP fixture was not released")
            return SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                json=lambda: {"id": 100, "name": "Fixture Show", "first_air_date": "2018-04-25", "seasons": []})
        finally:
            self.active = False

    def close(self):
        self.closed_during_get |= self.active
        self.close_count += 1


class EpisodeResearchCloseReliabilityTests(_OfflineCase):
    def test_false_client_close_can_retry_but_no_new_reads_are_allowed(self):
        client = SimpleNamespace(close=Mock(side_effect=[False, True]), get=Mock())
        with patch.object(core, "TMDBClient", return_value=client):
            reader = core.EpisodeEvidenceReader(core.normalize_case(self.payload))
        self.addCleanup(reader.close)
        self.assertIs(reader.close(), False)
        with self.assertRaises(core.EpisodeResearchError) as raised:
            reader.inspect_candidate(0)
        self.assertEqual(raised.exception.code, "reader_closed")
        client.get.assert_not_called()
        self.assertIs(reader.close(), True)
        self.assertIs(reader.close(), True)
        self.assertEqual(client.close.call_count, 2)

    def test_real_tmdb_client_refused_close_is_retried_after_inflight_read(self):
        session = _BlockedSession()
        client = self.real_client(session)
        self.addCleanup(client.close)
        with patch.object(core, "TMDBClient", return_value=client):
            reader = core.EpisodeEvidenceReader(core.normalize_case(self.payload))
        self.addCleanup(reader.close)
        with ThreadPoolExecutor(max_workers=1) as pool:
            work = pool.submit(reader.inspect_candidate, 0)
            try:
                self.assertTrue(session.entered.wait(1))
                self.assertIs(reader.close(), False)
                self.assertEqual(session.close_count, 0)
                with self.assertRaises(core.EpisodeResearchError) as raised:
                    reader.inspect_candidate(0)
                self.assertEqual(raised.exception.code, "reader_closed")
                self.assertEqual(session.get_count, 1)
            finally:
                session.release.set()
            with self.assertRaises(core.EpisodeResearchError) as raised:
                work.result(timeout=1)
            self.assertEqual(raised.exception.code, "reader_closed")
        self.assertIs(reader.close(), True)
        self.assertIs(reader.close(), True)
        self.assertEqual(session.close_count, 1)
        self.assertFalse(session.closed_during_get)

    def assert_service_worker_owns_close(self, *, outer_deadline=False):
        session = _BlockedSession()
        client = self.real_client(session)
        self.addCleanup(client.close)
        worker_closed = threading.Event()
        attempts = []
        main_thread = threading.current_thread().name

        class RecordingReader(core.EpisodeEvidenceReader):
            def close(self):
                attempts.append((threading.current_thread().name, session.active))
                try:
                    return super().close()
                finally:
                    if threading.current_thread().name == "episode-research-read":
                        worker_closed.set()

        async def offline_runner(payload, *, reader):
            model = ScriptedModel(rounds(call("inspect_case"), call("inspect_candidate", {"candidate_index": 0})))
            return await runner.research_episode_case_async(payload, reader=reader, model=model)

        # 先完成本地解析冷初始化；只缩时runner，保持真实service/reader/HTTP生命周期。
        core.normalize_case(self.payload)
        started = time.monotonic()
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(core, "TMDBClient", return_value=client))
                stack.enter_context(patch.object(runner, "_TIMEOUT_SECONDS", 2 if outer_deadline else 0.12))
                if outer_deadline:
                    # 本用例验证在途读取的关闭所有权，不要求模型/线程在120ms内启动。
                    # 等HTTP已进入后再触发真实wait_for超时，避免冷初始化抢先耗尽预算。
                    real_wait_for = asyncio.wait_for
                    armed = False

                    async def expire_during_read(awaitable, timeout):
                        nonlocal armed, started
                        if armed:
                            return await real_wait_for(awaitable, timeout)
                        armed = True
                        task = asyncio.create_task(awaitable)
                        try:
                            self.assertTrue(await asyncio.to_thread(session.entered.wait, 1))
                            started = time.monotonic()
                            return await real_wait_for(task, 0.03)
                        finally:
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)

                    stack.enter_context(patch.object(service.asyncio, "wait_for", expire_during_read))
                    stack.enter_context(patch.object(service, "_TOTAL_TIMEOUT_SECONDS", 2))
                    stack.enter_context(patch.object(runner, "_CLEANUP_SECONDS", 0.03))
                result = service.research_confirmation_episodes(self.payload, runner=offline_runner, reader_factory=RecordingReader)
            self.assertEqual(result["reason_code"], "research_timeout")
            self.assertLess(time.monotonic()-started, 1.0)
            self.assertTrue(session.entered.is_set())
            self.assertTrue(session.active)
            self.assertEqual(session.close_count, 0)
            self.assertFalse(any(name == main_thread for name, _active in attempts), attempts)
        finally:
            session.release.set()
            self.assertTrue(worker_closed.wait(1), attempts)
        self.assertEqual(attempts, [("episode-research-read", False)])
        self.assertEqual(session.close_count, 1)
        self.assertFalse(session.closed_during_get)

    def test_service_does_not_close_runner_owned_reader_after_timeout(self):
        self.assert_service_worker_owns_close()

    def test_service_outer_deadline_cancellation_keeps_worker_as_only_close_owner(self):
        self.assert_service_worker_owns_close(outer_deadline=True)

    def test_service_closes_reader_when_async_runner_never_started(self):
        client = SimpleNamespace(close=Mock(return_value=True), get=Mock())
        runner_started = []

        async def offline_runner(payload, *, reader):
            runner_started.append(True)
            raise AssertionError("runner must not start")

        def refuse_start(coroutine):
            coroutine.close()  # 模拟loop启动失败，不留下未await的协程警告。
            raise RuntimeError("fixture loop startup failed")

        with patch.object(core, "TMDBClient", return_value=client), patch.object(service.asyncio, "run", side_effect=refuse_start):
            result = service.research_confirmation_episodes(self.payload, runner=offline_runner)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(runner_started, [])
        client.close.assert_called_once()
        client.get.assert_not_called()

    def test_injected_external_client_is_not_closed_by_reader(self):
        client = EvidenceClient()
        client.close = Mock()
        reader = self.reader(client=client)
        self.assertIs(reader.close(), True)
        self.assertIs(reader.close(), True)
        client.close.assert_not_called()

    def test_synchronous_cached_revalidation_still_closes_its_own_reader(self):
        proposal = self.cached_proposal()
        readers = []

        def factory(case, **kwargs):
            reader = core.EpisodeEvidenceReader(case, client=EvidenceClient(), **kwargs)
            close = Mock(wraps=reader.close)
            reader.close = close
            readers.append((reader, close))
            return reader

        current = service.revalidate_episode_research_receipt(
            self.payload, service.proposal_receipt(proposal), reader_factory=factory)
        self.assertEqual(current["status"], "verified")
        self.assertEqual(len(readers), 1)
        reader, close = readers[0]
        close.assert_called_once()
        with self.assertRaises(core.EpisodeResearchError) as raised:
            reader.inspect_candidate(0)
        self.assertEqual(raised.exception.code, "reader_closed")


class _Clock:
    def __init__(self):
        self.value = time.monotonic()

    def monotonic(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds

    def scope(self):
        stack = ExitStack()
        stack.enter_context(patch.object(service, "time", self))
        stack.enter_context(patch.object(core, "time", self))
        return stack


class EpisodeResearchDeadlineReliabilityTests(_OfflineCase):
    def test_expired_receipt_deadline_does_not_construct_reader(self):
        proposal = self.cached_proposal()
        factory = Mock(side_effect=AssertionError("expired deadline must not create reader"))
        with self.assertRaises(core.EpisodeResearchError) as raised:
            service.revalidate_episode_research_receipt(
                self.payload, service.proposal_receipt(proposal),
                reader_factory=factory, deadline_at=time.monotonic()-1)
        self.assertEqual(raised.exception.code, "research_timeout")
        factory.assert_not_called()

    def test_explicit_receipt_deadline_bounds_every_tmdb_request(self):
        proposal = self.cached_proposal()
        clock = _Clock()
        deadline = clock.monotonic()+10
        received = []

        class TimedClient(EvidenceClient):
            def get(self, path, **kwargs):
                received.append(kwargs["deadline_at"])
                clock.advance(0.25)
                return super().get(path, **kwargs)

        def factory(case, **kwargs):
            return core.EpisodeEvidenceReader(case, client=TimedClient(), **kwargs)

        with clock.scope():
            current = service.revalidate_episode_research_receipt(
                self.payload, service.proposal_receipt(proposal),
                reader_factory=factory, deadline_at=deadline)
        self.assertEqual(current["status"], "verified")
        self.assertTrue(received)
        self.assertTrue(all(value <= deadline for value in received), received)

    def test_stale_cache_consumes_budget_before_new_runner_starts(self):
        self.assertEqual(service._TOTAL_TIMEOUT_SECONDS, 95.0)
        self.cached_proposal()
        clock = _Clock()
        deadline = clock.monotonic()+95
        readers = []
        waits = []
        runner_started = []
        real_wait_for = asyncio.wait_for

        class StaleClient(EvidenceClient):
            def get(self, path, **kwargs):
                clock.advance(8)
                result = super().get(path, **kwargs)
                if path == "/tv/100/season/0":
                    result["episodes"][0]["id"] = 9999
                return result

        def factory(case, **kwargs):
            reader = core.EpisodeEvidenceReader(
                case, client=StaleClient() if not readers else EvidenceClient(), **kwargs)
            readers.append(reader)
            return reader

        async def offline_runner(payload, *, reader):
            try:
                runner_started.append(clock.monotonic())
                return {"status": "abstained", "proposal": None, "reason_code": "insufficient_evidence"}
            finally:
                reader.close()  # 注入runner也遵循交接后的关闭所有权契约。

        async def timed_wait_for(awaitable, timeout):
            waits.append((timeout, clock.monotonic()))
            return await real_wait_for(awaitable, timeout=timeout)

        with clock.scope(), patch.object(service, "_TOTAL_TIMEOUT_SECONDS", 95.0), patch.object(
            service.asyncio, "wait_for", side_effect=timed_wait_for):
            result = service.research_confirmation_episodes(
                self.payload, runner=offline_runner, reader_factory=factory)
        self.assertEqual(result["reason_code"], "insufficient_evidence")
        self.assertEqual(len(readers), 2)
        self.assertEqual(len(runner_started), 1)
        self.assertGreater(runner_started[0], deadline-95)
        self.assertTrue(waits)
        self.assertTrue(all(0 < timeout <= deadline-start for timeout, start in waits), waits)
        self.assertTrue(all(reader._deadline <= deadline for reader in readers))

    def test_cache_exhausting_total_deadline_cannot_start_another_runner(self):
        self.cached_proposal()
        clock = _Clock()
        deadline = clock.monotonic()+95
        readers = []
        model_called = []

        class DeadlineClient(EvidenceClient):
            def get(self, path, **kwargs):
                # 模拟在途同步读取退出时入口总预算已耗尽，不进行真实等待。
                clock.value = deadline+0.01
                return super().get(path, **kwargs)

        def factory(case, **kwargs):
            reader = core.EpisodeEvidenceReader(case, client=DeadlineClient(), **kwargs)
            readers.append(reader)
            return reader

        async def forbidden_runner(payload, *, reader):
            model_called.append(True)
            reader.close()
            return {"status": "abstained", "reason_code": "incorrectly_restarted"}

        with clock.scope(), patch.object(service, "_TOTAL_TIMEOUT_SECONDS", 95.0):
            result = service.research_confirmation_episodes(
                self.payload, runner=forbidden_runner, reader_factory=factory)
        self.assertEqual(result["reason_code"], "research_timeout")
        self.assertNotEqual(result["status"], "verified")
        self.assertEqual(len(readers), 1)
        self.assertEqual(model_called, [])

    def test_singleflight_wait_and_following_cache_validation_share_deadline(self):
        self.cached_proposal()
        clock = _Clock()
        deadline = clock.monotonic()+95
        requests = []
        lease = SimpleNamespace(tracked=True, owner=False)
        waited = []

        def wait(_lease, *, timeout):
            waited.append(timeout)
            clock.advance(40)
            return True

        flights = SimpleNamespace(reserve=Mock(return_value=lease), wait=Mock(side_effect=wait), finish=Mock())

        class TimedClient(EvidenceClient):
            def get(self, path, **kwargs):
                requests.append(kwargs["deadline_at"])
                return super().get(path, **kwargs)

        def factory(case, **kwargs):
            return core.EpisodeEvidenceReader(case, client=TimedClient(), **kwargs)

        with clock.scope(), patch.object(service, "_TOTAL_TIMEOUT_SECONDS", 95.0), patch.object(service, "_flights", flights):
            result = service.research_confirmation_episodes(self.payload, reader_factory=factory)
        self.assertEqual(result["status"], "verified")
        self.assertTrue(result["cached"])
        self.assertEqual(len(waited), 1)
        self.assertLessEqual(waited[0], 95)
        self.assertTrue(requests)
        self.assertTrue(all(value <= deadline for value in requests), requests)
        self.charge.assert_not_called()
        flights.finish.assert_called_once_with(lease)

    def test_singleflight_timeout_does_not_create_reader_or_charge_case(self):
        clock = _Clock()
        lease = SimpleNamespace(tracked=True, owner=False)
        flights = SimpleNamespace(reserve=Mock(return_value=lease), wait=Mock(return_value=False), finish=Mock())
        factory = Mock(side_effect=AssertionError("wait timeout must not create reader"))
        with clock.scope(), patch.object(service, "_TOTAL_TIMEOUT_SECONDS", 95.0), patch.object(service, "_flights", flights):
            result = service.research_confirmation_episodes(self.payload, reader_factory=factory)
        self.assertIn(result["reason_code"], {"research_timeout", "research_wait_timeout"})
        self.assertLessEqual(flights.wait.call_args.kwargs["timeout"], 95)
        factory.assert_not_called()
        self.charge.assert_not_called()


if __name__ == "__main__":
    unittest.main()
