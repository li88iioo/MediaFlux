"""深审：同协议、同算法只有一个实现；保留不同提供方的必要投影。"""
from __future__ import annotations

import unittest
from unittest import mock

from app.clients.base import MediaItem, MediaServerClient
from app.clients.emby import EmbyClient
from app.clients.http import HttpSecurityError
from app.clients.jellyfin import JellyfinClient
from app.indexers.providers.base import IndexerAdapter
from app.indexers.providers.btbtla import BTBtlaAdapter
from app.indexers.providers.mikan import MikanAdapter
from app.modules import media_identity, scraper
from app.modules.rss import RSSEngine
from app.repositories import media_subscriptions
from tests.support import isolated_test_database


class SingleTrackBusinessContractTests(unittest.TestCase):
    def test_subscription_check_has_no_unpaired_legacy_claim_path(self):
        self.assertFalse(hasattr(media_subscriptions, "claim_media_subscription_check"))
        with isolated_test_database():
            subscription_id = media_subscriptions.add_media_subscription(
                provider="tmdb", external_id="1", tmdb_id="1", media_type="tv", title="Test",
            )
            run_id = media_subscriptions.claim_media_subscription_check_run(subscription_id)
            assert isinstance(run_id, int)
            get_media_subscription_result = media_subscriptions.get_media_subscription(subscription_id)
            assert get_media_subscription_result is not None
            self.assertEqual(get_media_subscription_result["status"], "checking")
            self.assertEqual(media_subscriptions.list_media_subscription_runs(subscription_id=subscription_id)[0]["id"], run_id)
            self.assertIsNone(media_subscriptions.claim_media_subscription_check_run(subscription_id))

    def test_resume_operation_has_one_implementation_for_both_servers(self):
        self.assertIs(EmbyClient.continue_watching, JellyfinClient.continue_watching)
        self.assertIs(EmbyClient.continue_watching, MediaServerClient.continue_watching)

    def test_resume_keeps_server_specific_projection_and_bounded_request(self):
        for cls in (EmbyClient, JellyfinClient):
            with self.subTest(server=cls.__name__), cls("http://server.invalid", "synthetic") as client:
                projected = MediaItem(id="film", name="Film", type="Movie")
                with mock.patch.object(client, "_request", return_value={"Items": [{"Id": "film"}, None]}) as request, \
                        mock.patch.object(client, "_media_item", return_value=projected) as convert:
                    self.assertEqual(client.continue_watching("viewer", limit=100), [projected])
                self.assertEqual(request.call_args.args[0], "/Users/viewer/Items/Resume")
                self.assertEqual(request.call_args.kwargs["params"]["Limit"], 20)
                convert.assert_called_once_with({"Id": "film"})
                with mock.patch.object(client, "_request", return_value={"Items": {}}), self.assertRaises(ValueError):
                    client.continue_watching("viewer")

    def test_mirror_join_has_one_implementation(self):
        self.assertIs(MikanAdapter._join_known_host, BTBtlaAdapter._join_known_host)
        self.assertIs(MikanAdapter._join_known_host, IndexerAdapter._join_known_host)

    def test_mirror_relative_urls_and_wrong_hosts_preserve_existing_contract(self):
        for cls in (MikanAdapter, BTBtlaAdapter):
            with self.subTest(adapter=cls.__name__):
                adapter = cls(http=mock.Mock(), mirror_base_urls=("https://mirror.invalid/",))
                self.assertEqual(
                    adapter._join_known_host("/file", relative_base_url="https://mirror.invalid/"),
                    "https://mirror.invalid/file",
                )
                self.assertEqual(adapter._join_known_host("/file"), adapter.base_url + "file")
                with self.assertRaises(HttpSecurityError):
                    adapter._join_known_host("https://unregistered.invalid/file")

    def test_tmdb_alias_traversal_is_shared_but_title_normalization_is_not_merged(self):
        self.assertTrue(callable(getattr(media_identity, "tmdb_alias_values", None)))
        candidate = {"name": "正式名称", "original_name": "原名"}
        # RSS 按原始拼写做身份防误绑；识别使用既有比较键。不能强行合并两种去重语义。
        with mock.patch.object(media_identity, "tmdb_alias_values", return_value=[" Foo ", "foo", "Bar"]):
            self.assertEqual(RSSEngine._tmdb_title_values(candidate), ["正式名称", "原名", "Foo", "foo", "Bar"])
            self.assertEqual(scraper._candidate_aliases(candidate), ["Foo", "Bar"])

    def test_tmdb_alias_legacy_nested_payloads_are_not_lost(self):
        payload = {
            "name": "正式名称", "original_name": "原名",
            "aliases": ["简单别名", {"name": "对象别名"}],
            "alternative_titles": {"titles": [{"title": "旧译名"}]},
            "translations": {"translations": [{"data": {"name": "译名", "english_name": "English"}}]},
        }
        self.assertEqual(scraper._candidate_aliases(payload), ["简单别名", "对象别名", "旧译名", "译名", "English"])
        self.assertEqual(RSSEngine._tmdb_title_values(payload), ["正式名称", "原名", "简单别名", "对象别名", "旧译名", "译名", "English"])
