"""短生命周期 HTTP 客户端和整理服务的所有权回归测试。"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app.modules.directory_scrape as directory_scrape_module
import app.modules.local_media_service as local_media_module
from app.agent.guangya_schedule_config_actions import get_guangya_connection_status
from app.agent.indexer_actions import download_target_readiness
from app.agent.organize_actions import _run_guangya_organize_once
from app.clients.guangya import GuangYaClient, close_guangya_client
from app.clients.tmdb import TMDBClient
from app.clients.qbittorrent import QBittorrentClient
from app.modules.directory_scrape import DirectoryScrapeService
from app.modules.directory_scrape_errors import DirectoryScrapeRequestError
from app.modules.local_media_scheduler import LocalMediaScheduler
from app.modules.local_media_service import LocalMediaService, LocalMediaServiceError
from app.modules.nsfw import MetaTubeClient
from app.modules.organize import OrganizeRules, Organizer
from app.modules.scraper import TMDBScraper
from app.modules.share_transfer import _guangya_client_scope
from app.routes.logs_api import _closing_operation
from app.routes.tools_api import _close_scraper


class _LoginRaw:
    instances: list["_LoginRaw"] = []
    fail_login = False
    fail_close = False

    def __init__(self, *_args, **_kwargs) -> None:
        self.token = ""
        self.refresh_token_value = ""
        self.device_id = "test-device"
        self.token_expires_at = None
        self.close_calls = 0
        type(self).instances.append(self)

    def close(self) -> None:
        self.close_calls += 1
        if self.fail_close:
            raise RuntimeError("close failed")

    def login_sms_init(self, _phone: str) -> dict:
        return {"captcha_token": "captcha"}

    def login_sms_send(self, _phone: str, _captcha_token: str) -> dict:
        return {"verification_id": "verify"}

    def login_sms(self, _username: str, *, get_code) -> None:
        _ = get_code()
        if self.fail_login:
            raise RuntimeError("login failed")
        self.token = "access"
        self.refresh_token_value = "refresh"
        self.token_expires_at = 1_900_000_000


class _RotatingCredentialRaw:
    def __init__(self, access_token=None, refresh_token=None, device_id=None) -> None:
        self.token = str(access_token or "")
        self.refresh_token_value = str(refresh_token or "")
        self.device_id = str(device_id or "test-device")
        self.token_expires_at = None

    def refresh_token(self, _refresh_token=None):
        self.token = "rotated-access"
        self.refresh_token_value = "rotated-refresh"
        self.token_expires_at = 1_900_000_000
        return {
            "access_token": self.token,
            "refresh_token": self.refresh_token_value,
            "expires_at": self.token_expires_at,
        }

    @staticmethod
    def close() -> None:
        pass


class ClientResourceLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        _LoginRaw.instances = []
        _LoginRaw.fail_login = False
        _LoginRaw.fail_close = False

    def _guangya_client(self) -> tuple[tempfile.TemporaryDirectory, GuangYaClient]:
        temp_dir = tempfile.TemporaryDirectory()
        client = GuangYaClient(token_file=Path(temp_dir.name) / "token.json")
        return temp_dir, client

    def test_guangya_login_preflight_clients_close_without_masking_success(self) -> None:
        temp_dir, client = self._guangya_client()
        self.addCleanup(temp_dir.cleanup)
        _LoginRaw.fail_close = True
        with patch("app.clients.guangya._load_raw", return_value=_LoginRaw):
            self.assertEqual(client.login_init("13800138000")["captcha_token"], "captcha")
            self.assertEqual(
                client.send_sms("13800138000", "captcha")["verification_id"],
                "verify",
            )

        self.assertEqual([raw.close_calls for raw in _LoginRaw.instances], [1, 1])

    def test_guangya_failed_login_closes_temporary_sdk_client(self) -> None:
        temp_dir, client = self._guangya_client()
        self.addCleanup(temp_dir.cleanup)
        _LoginRaw.fail_login = True
        with patch("app.clients.guangya._load_raw", return_value=_LoginRaw):
            with self.assertRaisesRegex(RuntimeError, "login failed"):
                client.login("13800138000", "123456")

        self.assertEqual(len(_LoginRaw.instances), 1)
        self.assertEqual(_LoginRaw.instances[0].close_calls, 1)
        self.assertIsNone(client._raw)

    def test_guangya_successful_login_closes_replaced_client_and_owns_new_one(self) -> None:
        temp_dir, client = self._guangya_client()
        self.addCleanup(temp_dir.cleanup)
        previous = Mock()
        client._raw = previous
        with patch("app.clients.guangya._load_raw", return_value=_LoginRaw), patch.object(
            client, "_install_refresh_hook"
        ), patch.object(client, "_write_token_locked"), patch.object(
            client, "_advance_credentials_after_rotation"
        ):
            self.assertTrue(client.login("13800138000", "123456"))

        current = _LoginRaw.instances[0]
        previous.close.assert_called_once_with()
        self.assertIs(client._raw, current)
        self.assertEqual(current.close_calls, 0)
        client.close()
        client.close()
        self.assertEqual(current.close_calls, 1)

    def test_close_guangya_client_never_replaces_business_result(self) -> None:
        client = Mock()
        client.close.side_effect = RuntimeError("cleanup failed")
        self.assertFalse(close_guangya_client(client))
        client.close.assert_called_once_with()

    def test_guangya_close_failure_retains_raw_for_retry(self) -> None:
        temp_dir, client = self._guangya_client()
        self.addCleanup(temp_dir.cleanup)
        raw = _LoginRaw()
        client._raw = raw
        _LoginRaw.fail_close = True

        self.assertFalse(client.close())
        self.assertIs(client._raw, raw)
        _LoginRaw.fail_close = False
        self.assertTrue(client.close())
        self.assertIsNone(client._raw)
        self.assertEqual(raw.close_calls, 2)

    def test_tmdb_client_close_failure_retains_session_for_retry(self) -> None:
        session = Mock(proxies={})
        session.close.side_effect = [RuntimeError("close failed"), None]
        client = TMDBClient(api_key="key", session=session)

        self.assertFalse(client.close())
        self.assertFalse(client._closed)
        self.assertTrue(client.close())
        self.assertTrue(client._closed)
        self.assertEqual(session.close.call_count, 2)

    def test_tmdb_close_does_not_interrupt_inflight_request(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class BlockingSession:
            def __init__(self) -> None:
                self.proxies = {}
                self.close_calls = 0

            def get(self, *_args, **_kwargs):
                entered.set()
                if not release.wait(timeout=2):
                    raise AssertionError("request was not released")
                response = Mock(status_code=200)
                response.raise_for_status.return_value = None
                response.json.return_value = {"ok": True}
                return response

            def close(self):
                self.close_calls += 1

        session = BlockingSession()
        client = TMDBClient(api_key="key", session=session)
        results: list[dict] = []
        errors: list[BaseException] = []

        def request() -> None:
            try:
                results.append(client.get("/movie/1"))
            except BaseException as exc:  # pragma: no cover - assertion aid
                errors.append(exc)

        thread = threading.Thread(target=request)
        thread.start()
        self.assertTrue(entered.wait(timeout=2))
        self.assertFalse(client.close())
        self.assertEqual(session.close_calls, 0)
        release.set()
        thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertEqual(results, [{"ok": True}])
        self.assertTrue(client.close())
        self.assertEqual(session.close_calls, 1)

    def test_qbittorrent_close_is_idempotent(self) -> None:
        session = Mock()
        with patch("app.clients.qbittorrent.requests.Session", return_value=session):
            client = QBittorrentClient("http://127.0.0.1:8080")
        client.close()
        client.close()
        session.close.assert_called_once_with()

    def test_metatube_closes_only_owned_session(self) -> None:
        owned = Mock()
        with patch("app.modules.nsfw.requests.Session", return_value=owned):
            client = MetaTubeClient("http://127.0.0.1:8080")
        client.close()
        client.close()
        owned.close.assert_called_once_with()

        injected = Mock()
        MetaTubeClient("http://127.0.0.1:8080", session=injected).close()
        injected.close.assert_not_called()

    def test_metatube_close_failure_is_retryable(self) -> None:
        session = Mock()
        session.close.side_effect = [RuntimeError("close failed"), None]
        with patch("app.modules.nsfw.requests.Session", return_value=session):
            client = MetaTubeClient("http://127.0.0.1:8080")

        self.assertFalse(client.close())
        self.assertTrue(client.close())
        self.assertEqual(session.close.call_count, 2)

    def test_tmdb_scraper_closes_only_owned_client(self) -> None:
        owned = Mock(api_key="", base_url="https://api.themoviedb.org/3")
        with patch("app.modules.scraper.TMDBClient", return_value=owned):
            scraper = TMDBScraper()
        scraper.close()
        scraper.close()
        owned.close.assert_called_once_with()

        injected = Mock(api_key="", base_url="https://api.themoviedb.org/3")
        TMDBScraper(client=injected).close()
        injected.close.assert_not_called()

    def test_tmdb_scraper_close_failure_is_retryable(self) -> None:
        owned = Mock(api_key="", base_url="https://api.themoviedb.org/3")
        owned.close.side_effect = [False, True]
        with patch("app.modules.scraper.TMDBClient", return_value=owned):
            scraper = TMDBScraper()

        self.assertFalse(scraper.close())
        self.assertTrue(scraper.close())
        self.assertEqual(owned.close.call_count, 2)

    def test_organizer_closes_owned_dependencies_and_cached_recognizers(self) -> None:
        owned_client = Mock()
        owned_scraper = Mock()
        recognizer = Mock()
        with patch("app.modules.organize.GuangYaClient", return_value=owned_client), patch(
            "app.modules.organize.TMDBScraper", return_value=owned_scraper
        ):
            organizer = Organizer()
        organizer._nsfw_recognizers[("endpoint", "token", "", 8)] = recognizer
        organizer.close()
        organizer.close()

        owned_client.close.assert_called_once_with()
        owned_scraper.close.assert_called_once_with()
        recognizer.close.assert_called_once_with()

        injected_client = Mock()
        injected_scraper = Mock()
        Organizer(client=injected_client, scraper=injected_scraper).close()
        injected_client.close.assert_not_called()
        injected_scraper.close.assert_not_called()

    def test_organizer_retries_failed_recognizer_before_owned_dependencies(self) -> None:
        owned_client = Mock()
        owned_scraper = Mock()
        recognizer = Mock()
        recognizer.close.side_effect = [False, True]
        with patch("app.modules.organize.GuangYaClient", return_value=owned_client), patch(
            "app.modules.organize.TMDBScraper", return_value=owned_scraper
        ):
            organizer = Organizer()
        organizer._nsfw_recognizers[("endpoint", "token", "", 8)] = recognizer

        self.assertFalse(organizer.close())
        owned_client.close.assert_not_called()
        owned_scraper.close.assert_not_called()
        self.assertTrue(organizer.close())
        self.assertEqual(recognizer.close.call_count, 2)
        owned_client.close.assert_called_once_with()
        owned_scraper.close.assert_called_once_with()

    def test_local_media_close_waits_for_inflight_search(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        scraper = Mock()
        scraper.search_candidates.side_effect = lambda *_args: (
            entered.set(),
            release.wait(timeout=2),
            [],
        )[-1]
        scraper.close.return_value = True
        with patch(
            "app.modules.local_media_service.TMDBScraper", return_value=scraper,
        ):
            service = LocalMediaService()
        results: list[list[dict]] = []
        thread = threading.Thread(
            target=lambda: results.append(service.search("title", media_type="movie"))
        )
        thread.start()
        self.assertTrue(entered.wait(timeout=2))

        self.assertFalse(service.close())
        scraper.close.assert_not_called()
        release.set()
        thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [[]])
        self.assertTrue(service.close())
        scraper.close.assert_called_once_with()

    def test_directory_scrape_close_waits_for_inflight_search(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        client = Mock()
        client.close.return_value = True
        scraper = Mock()
        scraper.search_candidates.side_effect = lambda *_args: (
            entered.set(),
            release.wait(timeout=2),
            [],
        )[-1]
        scraper.close.return_value = True
        record = Mock()
        record.rules = OrganizeRules(nsfw_exclusive=False)
        record.inspection.suggested_query = "title"
        record.inspection.media_type = "movie"
        store = Mock()
        store.get_inspection.return_value = record
        with patch(
            "app.modules.directory_scrape.GuangYaClient", return_value=client,
        ), patch(
            "app.modules.directory_scrape.TMDBScraper", return_value=scraper,
        ):
            service = DirectoryScrapeService(store=store)
        results: list[list[dict]] = []
        thread = threading.Thread(
            target=lambda: results.append(
                service.search("owner", "inspection", "title", "movie")
            )
        )
        thread.start()
        self.assertTrue(entered.wait(timeout=2))

        self.assertFalse(service.close())
        client.close.assert_not_called()
        scraper.close.assert_not_called()
        release.set()
        thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [[]])
        self.assertTrue(service.close())
        client.close.assert_called_once_with()
        scraper.close.assert_called_once_with()

    def test_local_media_admitted_operation_can_finish_nested_work_while_closing(
        self,
    ) -> None:
        service = LocalMediaService(scraper=Mock())
        service._preview_locked = Mock(return_value={"status": "planned"})
        rejected: list[BaseException] = []

        def start_new_operation() -> None:
            try:
                with service._lifecycle_operation():
                    pass
            except BaseException as exc:  # pragma: no cover - assertion aid
                rejected.append(exc)

        with service._lifecycle_operation():
            self.assertFalse(service.close())
            self.assertEqual(
                service.preview("owner", "inspection"),
                {"status": "planned"},
            )
            thread = threading.Thread(target=start_new_operation)
            thread.start()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(rejected), 1)
            self.assertIsInstance(rejected[0], LocalMediaServiceError)

        self.assertEqual(service._lifecycle_state(), (True, False, 0))
        self.assertTrue(service.close())

    def test_directory_admitted_operation_keeps_its_recognizer_while_closing(
        self,
    ) -> None:
        service = DirectoryScrapeService(
            client=object(), scraper=Mock(), store=Mock(),
        )
        rules = OrganizeRules(
            nsfw_enabled=True,
            nsfw_exclusive=True,
            nsfw_metatube_endpoint="http://metatube.invalid",
        )
        rejected: list[BaseException] = []
        constructed = []

        class FakeRecognizer:
            def __init__(self, *_args, **_kwargs):
                self.close_calls = 0
                constructed.append(self)

            def close(self):
                self.close_calls += 1
                return True

        def start_new_operation() -> None:
            try:
                with service._lifecycle_operation():
                    pass
            except BaseException as exc:  # pragma: no cover - assertion aid
                rejected.append(exc)

        with patch("app.modules.nsfw.NsfwRecognizer", FakeRecognizer):
            with service._lifecycle_operation():
                with service._nsfw_recognizer_lease(rules) as recognizer:
                    self.assertIs(recognizer, constructed[0])
                    self.assertFalse(service.close())
                    with service._nsfw_recognizer_lease(rules) as nested:
                        self.assertIs(nested, recognizer)
                    thread = threading.Thread(target=start_new_operation)
                    thread.start()
                    thread.join(timeout=2)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(len(rejected), 1)
                    self.assertIsInstance(rejected[0], DirectoryScrapeRequestError)

        self.assertEqual(service._lifecycle_state(), (True, False, 0))
        self.assertEqual(constructed[0].close_calls, 1)
        self.assertTrue(service.close())

    def test_global_service_getters_replace_completed_deferred_close_once(self) -> None:
        local_old = LocalMediaService(scraper=Mock())
        directory_old = DirectoryScrapeService(
            client=object(), scraper=Mock(), store=Mock(),
        )

        class HealthyReplacement:
            @staticmethod
            def _lifecycle_state():
                return False, False, 0

        local_replacement = HealthyReplacement()
        directory_replacement = HealthyReplacement()

        with local_media_module._service_lock:
            saved_local = local_media_module._service
            local_media_module._service = local_old
        with directory_scrape_module._service_lock:
            saved_directory = directory_scrape_module._service
            directory_scrape_module._service = directory_old

        try:
            with local_old._lifecycle_operation():
                self.assertFalse(local_media_module.close_local_media_service())
                with self.assertRaises(LocalMediaServiceError):
                    local_media_module.get_local_media_service()
            with directory_old._lifecycle_operation():
                self.assertFalse(directory_scrape_module.close_directory_scrape_service())
                with self.assertRaises(DirectoryScrapeRequestError):
                    directory_scrape_module.get_directory_scrape_service()

            local_results = []
            directory_results = []
            with patch.object(
                local_media_module,
                "LocalMediaService",
                return_value=local_replacement,
            ) as local_factory, patch.object(
                directory_scrape_module,
                "DirectoryScrapeService",
                return_value=directory_replacement,
            ) as directory_factory:
                local_threads = [
                    threading.Thread(
                        target=lambda: local_results.append(
                            local_media_module.get_local_media_service()
                        )
                    )
                    for _ in range(4)
                ]
                directory_threads = [
                    threading.Thread(
                        target=lambda: directory_results.append(
                            directory_scrape_module.get_directory_scrape_service()
                        )
                    )
                    for _ in range(4)
                ]
                for thread in local_threads + directory_threads:
                    thread.start()
                for thread in local_threads + directory_threads:
                    thread.join(timeout=2)

                self.assertFalse(
                    any(thread.is_alive() for thread in local_threads + directory_threads)
                )
                self.assertEqual(local_results, [local_replacement] * 4)
                self.assertEqual(directory_results, [directory_replacement] * 4)
                local_factory.assert_called_once_with()
                directory_factory.assert_called_once_with(
                    store=directory_scrape_module._store
                )
        finally:
            with local_media_module._service_lock:
                local_media_module._service = saved_local
            with directory_scrape_module._service_lock:
                directory_scrape_module._service = saved_directory

    def test_directory_service_getter_rebuilds_stale_credential_runtime_once(self) -> None:
        class StaleService:
            def __init__(self) -> None:
                self.close_calls = 0

            @staticmethod
            def _lifecycle_state():
                return False, False, 0

            @staticmethod
            def _runtime_credentials_current():
                return False

            def close(self):
                self.close_calls += 1
                return True

        class HealthyReplacement:
            @staticmethod
            def _lifecycle_state():
                return False, False, 0

            @staticmethod
            def _runtime_credentials_current():
                return True

        stale = StaleService()
        replacement = HealthyReplacement()
        with directory_scrape_module._service_lock:
            saved = directory_scrape_module._service
            directory_scrape_module._service = stale

        try:
            results = []
            with patch.object(
                directory_scrape_module,
                "DirectoryScrapeService",
                return_value=replacement,
            ) as factory:
                threads = [
                    threading.Thread(
                        target=lambda: results.append(
                            directory_scrape_module.get_directory_scrape_service()
                        )
                    )
                    for _ in range(4)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=2)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(results, [replacement] * 4)
            self.assertEqual(stale.close_calls, 1)
            factory.assert_called_once_with(store=directory_scrape_module._store)
        finally:
            with directory_scrape_module._service_lock:
                directory_scrape_module._service = saved

    def test_directory_service_reloads_after_another_client_rotates_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "guangya-token.json"
            token_file.write_text(
                json.dumps({
                    "access_token": "initial-access",
                    "refresh_token": "initial-refresh",
                    "device_id": "test-device",
                    "expires_at": 1_900_000_000,
                }),
                encoding="utf-8",
            )
            replacement = None
            refresher = None
            stale = None
            with patch(
                "app.clients.guangya._load_raw",
                return_value=_RotatingCredentialRaw,
            ), patch.object(
                directory_scrape_module,
                "GuangYaClient",
                side_effect=lambda: GuangYaClient(token_file=token_file),
            ), patch.object(
                directory_scrape_module,
                "TMDBScraper",
                return_value=Mock(),
            ):
                stale = DirectoryScrapeService(
                    store=directory_scrape_module._store,
                )
                refresher = GuangYaClient(token_file=token_file)
                with directory_scrape_module._service_lock:
                    saved = directory_scrape_module._service
                    directory_scrape_module._service = stale
                try:
                    refresher.refresh_now()
                    self.assertFalse(stale.client.credentials_current)

                    replacement = directory_scrape_module.get_directory_scrape_service()

                    self.assertIsNot(replacement, stale)
                    self.assertIs(replacement.store, directory_scrape_module._store)
                    self.assertTrue(replacement.client.credentials_current)
                    self.assertEqual(stale._lifecycle_state(), (True, False, 0))
                finally:
                    with directory_scrape_module._service_lock:
                        current = directory_scrape_module._service
                        directory_scrape_module._service = saved
                    if current is not saved:
                        current.close()
                    refresher.close()
                    if stale is not None and not stale._closed:
                        stale.close()

    def test_local_media_preview_serializes_shared_organizer_state(self) -> None:
        service = LocalMediaService(scraper=Mock())
        first_entered = threading.Event()
        release_first = threading.Event()
        second_done = threading.Event()
        active = 0
        max_active = 0
        calls = 0
        guard = threading.Lock()

        def slow_preview(*_args, **_kwargs):
            nonlocal active, max_active, calls
            with guard:
                calls += 1
                call_number = calls
                active += 1
                max_active = max(max_active, active)
            try:
                if call_number == 1:
                    first_entered.set()
                    self.assertTrue(release_first.wait(timeout=2))
                return {"call": call_number}
            finally:
                with guard:
                    active -= 1

        service._preview_locked = slow_preview
        first = threading.Thread(target=lambda: service.preview("owner", "one"))

        def run_second() -> None:
            service.preview("owner", "two")
            second_done.set()

        second = threading.Thread(target=run_second)
        first.start()
        self.assertTrue(first_entered.wait(timeout=2))
        second.start()
        time.sleep(0.05)
        self.assertFalse(second_done.is_set())
        release_first.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(max_active, 1)

    def test_background_correction_wrapper_closes_on_success_and_error(self) -> None:
        service = Mock()
        callback = Mock(return_value={"ok": True})
        self.assertEqual(_closing_operation(service, callback)(), {"ok": True})
        service.close.assert_called_once_with()

        failing_service = Mock()
        failing_callback = Mock(side_effect=RuntimeError("write failed"))
        with self.assertRaisesRegex(RuntimeError, "write failed"):
            _closing_operation(failing_service, failing_callback)()
        failing_service.close.assert_called_once_with()

    def test_scraper_cleanup_tolerates_legacy_test_double_and_close_failure(self) -> None:
        _close_scraper(object())
        scraper = Mock()
        scraper.close.side_effect = RuntimeError("cleanup failed")
        _close_scraper(scraper)
        scraper.close.assert_called_once_with()

    def test_agent_readiness_checks_close_short_lived_guangya_clients(self) -> None:
        status_client = Mock(logged_in=False)
        with patch(
            "app.agent.guangya_schedule_config_actions.GuangYaClient",
            return_value=status_client,
        ):
            result = get_guangya_connection_status({})
        self.assertEqual(result.status, "not_configured")
        status_client.close.assert_called_once_with()

        indexer_client = Mock(logged_in=True)
        with patch(
            "app.agent.indexer_actions.GuangYaClient",
            return_value=indexer_client,
        ):
            self.assertEqual(download_target_readiness("guangya"), {"guangya": True})
        indexer_client.close.assert_called_once_with()

    def test_share_client_scope_closes_only_internal_client(self) -> None:
        owned = Mock()
        with patch("app.modules.share_transfer.GuangYaClient", return_value=owned):
            with _guangya_client_scope(None) as runtime:
                self.assertIs(runtime, owned)
        owned.close.assert_called_once_with()

        injected = Mock()
        with _guangya_client_scope(injected) as runtime:
            self.assertIs(runtime, injected)
        injected.close.assert_not_called()

    def test_agent_organize_transfers_client_only_after_task_acceptance(self) -> None:
        rules = OrganizeRules(target_dir_id="target")
        sources = [{"id": "source", "name": "Source"}]
        rejected_client = Mock(logged_in=True, credential_generation=3)
        rejected_manager = Mock()
        rejected_manager.start.return_value = {"ok": False, "error": "busy"}
        with patch(
            "app.agent.organize_actions.get_organize_manager",
            return_value=rejected_manager,
        ):
            result = _run_guangya_organize_once(
                sources, rules, "", rejected_client,
            )
        self.assertFalse(result.ok)
        rejected_client.close.assert_called_once_with()

        accepted_client = Mock(logged_in=True, credential_generation=4)
        accepted_manager = Mock()
        accepted_manager.start.return_value = {"ok": True, "task_id": "task"}
        with patch(
            "app.agent.organize_actions.get_organize_manager",
            return_value=accepted_manager,
        ):
            result = _run_guangya_organize_once(
                sources, rules, "", accepted_client,
            )
        self.assertTrue(result.ok)
        accepted_client.close.assert_not_called()
        accepted_manager.start.assert_called_once_with(
            sources,
            rules,
            trigger_type="manual",
            client=accepted_client,
            expected_credential_generation=4,
            take_client_ownership=True,
        )

    def test_local_media_services_release_only_owned_runtime_dependencies(self) -> None:
        owned_scraper = Mock()
        owned_organizer = Mock()
        with patch(
            "app.modules.local_media_service.TMDBScraper",
            return_value=owned_scraper,
        ), patch(
            "app.modules.local_media_service.Organizer",
            return_value=owned_organizer,
        ):
            service = LocalMediaService()
        service.close()
        service.close()
        owned_organizer.close.assert_called_once_with()
        owned_scraper.close.assert_called_once_with()

        injected_scraper = Mock()
        injected_organizer = Mock()
        with patch(
            "app.modules.local_media_service.Organizer",
            return_value=injected_organizer,
        ):
            service = LocalMediaService(scraper=injected_scraper)
        service.close()
        injected_organizer.close.assert_called_once_with()
        injected_scraper.close.assert_not_called()

    def test_local_media_scheduler_shutdown_closes_only_owned_service(self) -> None:
        owned_service = Mock()
        with patch(
            "app.modules.local_media_scheduler.LocalMediaService",
            return_value=owned_service,
        ):
            scheduler = LocalMediaScheduler()
        self.assertTrue(scheduler.shutdown())
        owned_service.close.assert_called_once_with()

        injected_service = Mock()
        scheduler = LocalMediaScheduler(service=injected_service)
        self.assertTrue(scheduler.shutdown())
        injected_service.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
