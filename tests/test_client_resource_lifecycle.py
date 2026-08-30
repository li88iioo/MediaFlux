"""短生命周期 HTTP 客户端和整理服务的所有权回归测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.agent.guangya_schedule_config_actions import get_guangya_connection_status
from app.agent.indexer_actions import _target_readiness
from app.agent.organize_actions import _run_guangya_organize_once
from app.clients.guangya import GuangYaClient, close_guangya_client
from app.clients.qbittorrent import QBittorrentClient
from app.modules.local_media_scheduler import LocalMediaScheduler
from app.modules.local_media_service import LocalMediaService
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
        close_guangya_client(client)
        client.close.assert_called_once_with()

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
            self.assertEqual(_target_readiness("guangya"), {"guangya": True})
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
