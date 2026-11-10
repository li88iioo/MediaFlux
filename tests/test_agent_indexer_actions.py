"""Media Agent 多站资源搜索与确认提交测试。"""

from __future__ import annotations
from typing import Any

import asyncio
import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

from app.modules.resource_candidates import _serialize_private_item, select_candidates
from app.indexers.service import IndexerService
from types import SimpleNamespace
from app.indexers.downloads import (
    DownloadSnapshotChanged, download_indexer_result, preflight_indexer_download_batch,
    preview_indexer_download_batch, submit_indexer_download_batch,
)
from app.indexers.errors import (
    IndexerResultExpired,
)
from app.indexers.models import (
    AggregatedIndexerResult,
    IndexerItem,
    IndexerMediaSearchRequest,
    IndexerCapabilities,
    IndexerProviderError,
    ResolvedDownload,
)
from app.modules.download_dispatcher import DownloadInput

_RESULT_ID = "opaque-result-1234"
_SECRET_MAGNET = "magnet:?xt=urn:btih:" + "a" * 40
_SECRET_TORRENT_URL = "https://private-indexer.example/resource.torrent"
_SECRET_DETAIL_URL = "https://private-indexer.example/details/1"


class FakeResultStore:
    def __init__(self, item: IndexerItem):
        self.item = item
        self.calls: list[str] = []

    def get(self, result_id: str) -> IndexerItem:
        self.calls.append(result_id)
        if result_id == "expired-result-1234":
            raise IndexerResultExpired()
        return self.item


class FakeIndexerService(IndexerService):
    def __init__(
        self,
        *,
        item: IndexerItem | None = None,
        items: list[IndexerItem] | None = None,
        enabled_site_ids: tuple[str, ...] = ("nyaa",),
        sites_attempted: tuple[str, ...] = ("nyaa", "broken"),
        sites_succeeded: tuple[str, ...] = ("nyaa",),
        partial: bool = True,
        private_results: bool = True,
        result_store=None,
    ):
        self.item = item or _resource_item()
        self.items = list(items) if items is not None else [self.item]
        self.result_store: Any = result_store if result_store is not None else FakeResultStore(self.item) if private_results else None
        self.enabled_site_ids = frozenset(enabled_site_ids)
        self.sites_attempted = sites_attempted
        self.sites_succeeded = sites_succeeded
        self.partial = partial
        self.registry: Any = SimpleNamespace(ids=lambda: tuple(self.enabled_site_ids), enabled_ids=lambda: (), get=lambda site: SimpleNamespace(site_name=site.upper(), capabilities=IndexerCapabilities(True), http_client_for_result=lambda _: None))
        self.search_calls: list[tuple[IndexerMediaSearchRequest, list[str] | None]] = []

    def site_ids_for_scope(self, scope="manual"):
        assert scope == "agent"
        return tuple(self.enabled_site_ids)

    def get_result(self, result_id, *, scope="manual"):
        assert scope == "agent"
        return self.result_store.get(result_id)

    async def search_media(self, request, site_ids=None, *, scope="manual", timeout_seconds=None, on_progress=None):
        assert scope == "agent"
        self.search_calls.append((request, site_ids))
        return AggregatedIndexerResult(
            query=request.title,
            page=request.page,
            items=self.items,
            sites_attempted=self.sites_attempted,
            sites_succeeded=self.sites_succeeded,
            errors=(
                [IndexerProviderError("broken", "unavailable", "站点暂不可用")]
                if self.partial
                else []
            ),
            partial=self.partial,
            cached=False,
            has_more=True,
        )


class FakeDownloadService:
    def __init__(self, item: IndexerItem):
        self.result_store = FakeResultStore(item)

    def get_result(self, result_id, *, scope="manual"):
        return self.result_store.get(result_id)

    async def resolve(self, _result_id: str, *, scope="manual") -> ResolvedDownload:
        return ResolvedDownload(kind="magnet", value=_SECRET_MAGNET)


def _resource_item(**overrides) -> IndexerItem:
    values = {
        "result_id": _RESULT_ID,
        "site_id": "nyaa",
        "site_name": "Nyaa",
        "title": "Demo Resource",
        "detail_url": _SECRET_DETAIL_URL,
        "category": "Anime",
        "size_text": "1.2 GB",
        "size_bytes": 1200000000,
        "seeders": 42,
        "leechers": 3,
        "downloads": 9,
        "published_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
        "download_state": "ready",
        "download_kinds": ("magnet", "torrent"),
        "magnet": _SECRET_MAGNET,
        "torrent_url": _SECRET_TORRENT_URL,
    }
    values.update(overrides)
    return IndexerItem(**values)


def _candidate_snapshot(*, count=2) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for position in range(1, count + 1):
        candidates.append(
            {
                "position": position,
                "result_id": f"opaque-result-{position:04d}",
                "title": f"Demo Resource {position}",
                "site_id": "nyaa",
                "site_name": "Nyaa",
                "size_text": "1.2 GB",
                "download_state": "ready",
                "_verification_context": None,
                "download_kinds": ["magnet"],
                "media_title": "",
                "episode_label": "",
                "subscription_number": None,
            }
        )
    return {
        "search_id": "rs_" + "a" * 16,
        "search_status": "success",
        "candidates": candidates,
        "_private_version": 1,
        "_private_items": [
            _serialize_private_item(item["result_id"], _resource_item(title=item["title"]))
            for item in candidates
        ],
    }


def _identity(arguments):
    return dict(arguments)


class AgentIndexerActionUnitTests(unittest.TestCase):













    def test_preview_is_safe_and_requires_ready_target(self):
        service = FakeIndexerService()
        arguments = {"result_id": _RESULT_ID, "target": "qb"}
        with (
            patch("app.indexers.downloads.config.get_bool", return_value=True),
            patch(
                "app.indexers.downloads.download_target_readiness",
                return_value={"qb": True},
            ),
        ):
            result, _context = preview_indexer_download_batch(service, [arguments["result_id"]], arguments["target"], scope="agent")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "confirmation_required")
        serialized = str(result)
        self.assertNotIn(_SECRET_MAGNET, serialized)
        self.assertNotIn(_SECRET_TORRENT_URL, serialized)
        self.assertNotIn(_SECRET_DETAIL_URL, serialized)
        with (
            patch("app.indexers.downloads.config.get_bool", return_value=True),
            patch(
                "app.indexers.downloads.download_target_readiness",
                return_value={"qb": False},
            ),
        ):
            unavailable, _context = preview_indexer_download_batch(service, [arguments["result_id"]], arguments["target"], scope="agent")
        self.assertFalse(unavailable["ok"])
        self.assertEqual(unavailable["status"], "not_configured")

    def test_preview_rejects_expired_and_non_downloadable_results(self):
        service = FakeIndexerService()
        with (
            patch("app.indexers.downloads.config.get_bool", return_value=True),
        ):
            expired, _context = preview_indexer_download_batch(service, ["expired-result-1234"], "qb", scope="agent")
            service.result_store.item = _resource_item(
                download_state="unavailable", download_kinds=()
            )
            blocked, _context = preview_indexer_download_batch(service, [_RESULT_ID], "qb", scope="agent")
        self.assertFalse(expired["ok"])
        self.assertEqual(expired["status"], "result_expired")
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["status"], "validation_error")

    def test_preview_expires_legacy_combined_ticket_without_rebinding_child_site(self):
        service = FakeIndexerService(
            item=_resource_item(
                site_id="btbtla",
                site_name="综合",
                detail_url="https://www.aipan.me/movie/legacy-resource",
            ),
            enabled_site_ids=("btbtla", "aipan"),
        )
        with (
            patch("app.indexers.downloads.config.get_bool", return_value=True),
            patch("app.indexers.downloads.download_target_readiness") as readiness,
        ):
            expired, _context = preview_indexer_download_batch(service, [_RESULT_ID], "qb", scope="agent")

        self.assertFalse(expired["ok"])
        self.assertEqual(expired["status"], "result_expired")
        assert expired["error"] is not None
        self.assertIn("旧资源确认已过期，请重新搜索", expired["error"])
        readiness.assert_not_called()

    def test_confirmation_context_changes_when_resource_changes(self):
        service = FakeIndexerService()
        arguments = {"result_id": _RESULT_ID, "target": "qb"}
        with (
            patch("app.indexers.downloads.config.get_bool", return_value=True),
            patch(
                "app.indexers.downloads.download_target_readiness",
                return_value={"qb": True},
            ),
        ):
            first = preview_indexer_download_batch(service, [arguments["result_id"]], arguments["target"], scope="agent")[1]
            service.result_store.item = _resource_item(title="Changed Resource")
            second = preview_indexer_download_batch(service, [arguments["result_id"]], arguments["target"], scope="agent")[1]
        self.assertNotEqual(first, second)
        self.assertEqual((first["resources"][0]["title"], second["resources"][0]["title"]), (service.item.title, "Changed Resource"))
        self.assertNotIn(_SECRET_MAGNET, str((first, second)))

    def test_submit_resource_returns_sanitized_public_result(self):
        service = FakeDownloadService(_resource_item())
        with patch("app.indexers.downloads._persist_and_dispatch", return_value=(
            {"created": True}, 12, {"status": "partial", "succeeded": ["qb"], "failed": ["guangya"],
                                  "error": "private backend error", "results": {"qb": {"token": "secret"}}},
        )) as dispatch:
            snapshot = preflight_indexer_download_batch(service, [_RESULT_ID], "both", scope="agent")
            result = asyncio.run(submit_indexer_download_batch(
                service, [_RESULT_ID], "both", expected_snapshot=snapshot, origin_namespace="agent", scope="agent",
            ))
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "partial")
        item = result["items"][0]
        self.assertEqual(set(item), {"result_id", "request_id", "created", "target", "status",
                                    "succeeded", "failed", "duplicate", "ok", "error"})
        self.assertEqual((result["total"], result["succeeded"], result["failed"]), (1, 1, 0))
        self.assertEqual((item["request_id"], item["status"], item["succeeded"], item["failed"]),
                         (12, "partial", ["qb"], ["guangya"]))
        self.assertNotIn("private backend error", str(result))
        self.assertNotIn("secret", str(result))
        dispatch.assert_called_once()
        self.assertEqual(dispatch.call_args.args[1:3], ("agent:nyaa", "both"))

    def test_submit_resource_maps_duplicate_failure_and_internal_error(self):
        service = FakeDownloadService(_resource_item())
        for outcome, expected_status in (("duplicate", "conflict"), ("failed", "unavailable")):
            with self.subTest(outcome=outcome), patch("app.indexers.downloads._persist_and_dispatch", return_value=(
                {"created": False}, 12, {"status": outcome, "succeeded": [], "failed": [], "duplicate": outcome == "duplicate",
                                       "error": "private backend detail"},
            )):
                snapshot = preflight_indexer_download_batch(service, [_RESULT_ID], "qb", scope="agent")
                result = asyncio.run(submit_indexer_download_batch(
                    service, [_RESULT_ID], "qb", expected_snapshot=snapshot, scope="agent",
                ))
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], expected_status)
            self.assertEqual(result["items"][0]["status"], outcome)
            self.assertNotIn("private backend detail", str(result))
        with patch("app.indexers.downloads._persist_and_dispatch", side_effect=RuntimeError("secret internal error")):
            snapshot = preflight_indexer_download_batch(service, [_RESULT_ID], "qb", scope="agent")
            result = asyncio.run(submit_indexer_download_batch(
                service, [_RESULT_ID], "qb", expected_snapshot=snapshot, scope="agent",
            ))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("secret internal error", str(result))

    def test_submit_resource_preserves_manual_review_without_private_error(self):
        service = FakeDownloadService(_resource_item())
        with patch("app.indexers.downloads._persist_and_dispatch", return_value=(
            {"created": True}, 12, {"status": "manual_review", "succeeded": [], "failed": ["guangya"],
                                  "error": "private timeout magnet:?xt=urn:btih:secret"},
        )):
            snapshot = preflight_indexer_download_batch(service, [_RESULT_ID], "guangya", scope="agent")
            result = asyncio.run(submit_indexer_download_batch(
                service, [_RESULT_ID], "guangya", expected_snapshot=snapshot, scope="agent",
            ))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "review_required")
        self.assertEqual((result["review_required"], result["failed"]), (1, 0))
        self.assertEqual(result["items"][0]["status"], "manual_review")
        self.assertIn("勿直接重复提交", result["error"])
        self.assertNotIn("private timeout", str(result))
        self.assertNotIn("magnet:", str(result))

    def test_single_resource_preserves_safe_guangya_rejection_reason(self):
        service = FakeDownloadService(_resource_item())
        with patch("app.indexers.downloads._persist_and_dispatch", return_value=(
            {"created": True}, 88, {"status": "failed", "succeeded": [], "failed": ["guangya"],
                                  "error": "上游文件违规 https://private.example/token=SECRET"},
        )):
            snapshot = preflight_indexer_download_batch(service, [_RESULT_ID], "guangya", scope="agent")
            result = asyncio.run(submit_indexer_download_batch(
                service, [_RESULT_ID], "guangya", expected_snapshot=snapshot, scope="agent",
            ))
        self.assertFalse(result["ok"])
        self.assertEqual(result["items"][0]["error"], "光鸭返回：文件违规")
        self.assertEqual(result["items"][0]["request_id"], 88)
        self.assertNotIn("SECRET", str(result))
        self.assertNotIn("https://", str(result))

    def test_submit_resource_batch_counts_manual_review_separately(self):
        service = FakeDownloadService(_resource_item())
        result_ids = [_RESULT_ID, "opaque-result-5678"]
        with patch("app.indexers.downloads._persist_and_dispatch", side_effect=[
            ({"created": True}, number, {"status": "manual_review", "succeeded": [], "failed": ["guangya"]})
            for number in (12, 13)
        ]):
            snapshot = preflight_indexer_download_batch(service, result_ids, "guangya", scope="agent")
            result = asyncio.run(submit_indexer_download_batch(
                service, result_ids, "guangya", expected_snapshot=snapshot, scope="agent",
            ))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "review_required")
        self.assertEqual(result["review_required"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertEqual([item["result_id"] for item in result["items"]], result_ids)
        self.assertIn("勿直接重复提交", result["error"])

    def test_shared_download_service_dispatches_only_server_resolved_value(self):
        item = _resource_item(download_kinds=("magnet",))
        service = FakeDownloadService(item)
        normalized = DownloadInput(kind="magnet", title="磁力任务", source_value=_SECRET_MAGNET)
        with (
            patch(
                "app.indexers.downloads.normalize_download_url", return_value=normalized
            ) as normalize,
            patch("app.indexers.downloads.request_keys", return_value=("req:key",)),
            patch(
                "app.indexers.downloads.db.get_download_request_by_request_key",
                return_value=None,
            ),
            patch(
                "app.indexers.downloads.db.get_download_request_by_request_keys",
                return_value=None,
            ),
            patch(
                "app.indexers.downloads.create_request",
                return_value={"id": 7, "created": True},
            ) as create,
            patch(
                "app.indexers.downloads.dispatch_request",
                return_value={"status": "submitted", "succeeded": ["qb"], "failed": []},
            ) as dispatch,
        ):
            result = asyncio.run(download_indexer_result(service, _RESULT_ID, "qb"))
        normalize.assert_called_once_with(_SECRET_MAGNET)
        create.assert_called_once_with(
            replace(normalized, display_title=item.title), "", "", origin="indexer:nyaa", user_id=""
        )
        dispatch.assert_called_once_with(7, "qb", qb_save_path="", qb_category="")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "submitted")

    def test_shared_download_service_marks_agent_origin_namespace(self):
        item = _resource_item(download_kinds=("magnet",))
        service = FakeDownloadService(item)
        normalized = DownloadInput(kind="magnet", title="磁力任务", source_value=_SECRET_MAGNET)
        with (
            patch(
                "app.indexers.downloads.normalize_download_url", return_value=normalized
            ),
            patch("app.indexers.downloads.request_keys", return_value=("req:key",)),
            patch(
                "app.indexers.downloads.db.get_download_request_by_request_key",
                return_value=None,
            ),
            patch(
                "app.indexers.downloads.db.get_download_request_by_request_keys",
                return_value=None,
            ),
            patch(
                "app.indexers.downloads.create_request",
                return_value={"id": 8, "created": True},
            ) as create,
            patch(
                "app.indexers.downloads.dispatch_request",
                return_value={"status": "submitted", "succeeded": ["qb"], "failed": []},
            ),
        ):
            result = asyncio.run(
                download_indexer_result(
                    service, _RESULT_ID, "qb", origin_namespace="agent"
                )
            )
        create.assert_called_once_with(
            replace(normalized, display_title=item.title), "", "", origin="agent:nyaa", user_id=""
        )
        self.assertTrue(result["ok"])


class IndexerReceivingSourceTests(unittest.TestCase):
    """合成资源，仅验证来源路由，不访问索引站或下载后端。"""

    def setUp(self):
        self.enterContext(patch("app.indexers.downloads.guangya_download_credential_generation", return_value=1))

    def test_classified_cloud_resource_uses_configured_source_on_shared_dispatch(self):
        for site, target, routed in (("sukebei", "guangya", True), ("sukebei", "both", True),
                                     ("sukebei", "qb", False), ("nyaa", "guangya", False)):
            with (self.subTest(site=site, target=target),
                patch("app.modules.organize_sources.list_nsfw_download_sources", return_value=[{"id": "source-123", "name": "FixtureSpecial"}]),
                patch("app.indexers.downloads.submit_download_input", return_value={
                    "request_id": 7, "created": True, "summary": {"duplicate": False,
                    "succeeded": [target], "failed": [], "status": "submitted", "error": ""},
                }) as submit,
            ):
                result = asyncio.run(download_indexer_result(
                    FakeDownloadService(_resource_item(site_id=site)), _RESULT_ID, target,
                    origin_namespace="agent", scope="agent",
                ))
                self.assertTrue(result["ok"])
                self.assertEqual(submit.call_args.kwargs.get("gy_target_dir", ""), "source-123" if routed else "0" if target == "guangya" else "")
                self.assertEqual(submit.call_args.kwargs.get("gy_target_name", ""), "FixtureSpecial" if routed else "默认目录" if target == "guangya" else "")

    def test_missing_or_ambiguous_source_never_falls_back_to_normal_inbox(self):
        from app.indexers.downloads import download_indexer_result_public
        for sources in ([], [{"id": "1", "name": "A"}, {"id": "2", "name": "B"}]):
            with (self.subTest(sources=sources),
                patch("app.modules.organize_sources.list_nsfw_download_sources", return_value=sources),
                patch("app.indexers.downloads.submit_download_input") as submit,
            ):
                result = asyncio.run(download_indexer_result_public(
                    FakeDownloadService(_resource_item(site_id="sukebei")), _RESULT_ID, "guangya", scope="agent",
                ))
                self.assertFalse(result["ok"])
                self.assertTrue(result["pre_submission_failure"])
                self.assertIn("NSFW", result["error"])
                submit.assert_not_called()

    def test_single_and_batch_confirmation_bind_actual_receiving_source(self):
        for result_ids in ([_RESULT_ID], [_RESULT_ID, "opaque-result-5678"]):
            service = FakeDownloadService(_resource_item(site_id="sukebei"))
            with (self.subTest(result_ids=result_ids),
                patch("app.indexers.downloads.config.get_bool", return_value=True),
                patch("app.indexers.downloads.download_target_readiness", return_value={"guangya": True}),
                patch("app.modules.organize_sources.list_nsfw_download_sources", return_value=[{"id": "source-123", "name": "FixtureSpecial"}]) as sources,
                patch("app.indexers.downloads.submit_download_input") as submit,
            ):
                preview, token = preview_indexer_download_batch(service, result_ids, "guangya", scope="agent")
                self.assertTrue(preview["ok"])
                self.assertEqual(preview["data"]["resources"][0]["receiving_folder"], "FixtureSpecial")
                self.assertNotIn("source-123", str(preview))
                sources.return_value = [{"id": "different-source", "name": "FixtureSpecial"}]
                with self.assertRaises(DownloadSnapshotChanged) as caught:
                    asyncio.run(submit_indexer_download_batch(service, result_ids, "guangya", expected_snapshot=token, scope="agent"))
                self.assertEqual(caught.exception.code, "confirmation_stale")
                submit.assert_not_called()

    def test_resource_or_defaults_changing_during_resolution_never_reach_dispatch(self):
        from app import config
        from app.indexers.downloads import DownloadSnapshotChanged, preflight_indexer_download
        from app.modules.offline import OfflineRules

        for target, field in (("qb", "title"), ("qb", "TG_QB_SAVE_PATH"), ("qb", "TG_QB_CATEGORY"),
                              ("guangya", "target_dir_id"), ("guangya", "secondary_dir_id")):
            with self.subTest(target=target, field=field):
                service = FakeDownloadService(_resource_item())
                values = {"TG_QB_SAVE_PATH": "/frozen", "TG_QB_CATEGORY": "original",
                          "QB_PASSWORD": "fixture-private-password"}
                rules = replace(OfflineRules.from_config(), target_dir_id="original", secondary_enabled=True,
                                secondary_dir_id="secondary-original", secondary_keywords=("Demo",))
                original_get = config.get
                with (
                    patch.object(config, "get", side_effect=lambda key, default="": values.get(key, original_get(key, default))),
                    patch("app.modules.offline.OfflineRules.from_config", return_value=rules) as routing,
                    patch("app.indexers.downloads.download_target_readiness", return_value={target: True}),
                    patch("app.indexers.downloads.submit_download_input") as submit,
                ):
                    frozen = preflight_indexer_download(service, _RESULT_ID, target, scope="agent")
                    serialized = json.dumps(frozen)
                    for secret in ("fixture-private-password", _SECRET_MAGNET, _SECRET_TORRENT_URL, _SECRET_DETAIL_URL):
                        self.assertNotIn(secret, serialized)

                    async def resolve(_result_id: str, *, scope: str = "manual") -> ResolvedDownload:
                        if field == "title":
                            service.result_store.item = replace(service.result_store.item, title="changed candidate")
                        elif target == "qb":
                            values[field] = "changed"
                        else:
                            routing.return_value = (replace(rules, target_dir_id="changed") if field == "target_dir_id"
                                                    else replace(rules, secondary_dir_id="changed"))
                        return ResolvedDownload("magnet", _SECRET_MAGNET)

                    service.resolve = resolve
                    with self.assertRaises(DownloadSnapshotChanged):
                        asyncio.run(download_indexer_result(service, _RESULT_ID, target, scope="agent",
                                                            expected_snapshot=json.loads(serialized)))
                    submit.assert_not_called()

    def test_receiving_summary_combines_selected_routes_without_false_default(self):
        from app.modules.offline import OfflineRules
        service = FakeIndexerService()
        service.get_result = lambda result_id, **_kw: _resource_item(site_id="sukebei" if result_id == "special" else "nyaa")
        with (
            patch("app.modules.offline.OfflineRules.from_config", return_value=replace(OfflineRules.from_config(), target_dir_id="normal", target_dir_name="Inbox", secondary_enabled=False)),
            patch("app.modules.organize_sources.list_nsfw_download_sources", return_value=[{"id": "special", "name": "FixtureSpecial"}]),
            patch("app.modules.missing_resources.config.get_bool", return_value=True),
            patch("app.indexers.runtime.get_indexer_service", return_value=service),
            patch("app.indexers.downloads.download_target_readiness", return_value={"guangya": True}),
        ):
            special, _ = preview_indexer_download_batch(service, ["special"], "guangya", scope="agent")
            combined, _ = preview_indexer_download_batch(service, ["special", "normal"], "guangya", scope="agent")
        self.assertEqual(special["data"]["receiving_folders"], ["光鸭：FixtureSpecial / 每任务隔离目录"])
        self.assertEqual(set(combined["data"]["receiving_folders"]), {"光鸭：FixtureSpecial / 每任务隔离目录", "光鸭：Inbox / 每任务隔离目录"})

    def test_classification_survives_persisted_staging_and_retry_without_remote_writes(self):
        from types import SimpleNamespace

        from app import database as db
        from app.modules import download_dispatcher as dispatcher
        from app.modules.organize_rules import OrganizeRules
        from tests.support import isolated_test_database

        def offline(_url, **kwargs):
            self.assertEqual(kwargs["target_dir_id"], "special-source")
            kwargs["on_staging_created"]({"id": "stage-7", "parent_id": "special-source", "name": "MF-7-Fixture"})
            return {"ok": True, "task_ids": ["fixture-task"], "decision": {"target_dir_id": "stage-7", "target_dir_name": "Fixture"},
                    "staging": {"isolated": True, "parent_id": "special-source", "name": "MF-7-Fixture"}}

        with (
            isolated_test_database(),
            patch("app.modules.organize_sources.list_nsfw_download_sources", return_value=[{"id": "special-source", "name": "FixtureSpecial"}]),
            patch.object(dispatcher, "_recover_guangya_magnet_torrent", return_value=None),
            patch.object(dispatcher, "analyze_offline_url", return_value=SimpleNamespace(allowed=True, reason="")),
            patch.object(dispatcher, "submit_offline", side_effect=offline) as backend,
        ):
            service = FakeDownloadService(_resource_item(site_id="sukebei"))
            submitted = asyncio.run(download_indexer_result(service, _RESULT_ID, "guangya", origin_namespace="agent", scope="agent"))
            self.assertTrue(submitted["ok"], submitted)
            row = db.get_download_request(submitted["request_id"])
            assert row is not None
            self.assertEqual((row["gy_requested_target_dir"], row["gy_requested_nsfw"]), ("special-source", 1))
            self.assertEqual((row["gy_staging_parent_dir"], row["gy_target_dir"]), ("special-source", "stage-7"))
            rules = OrganizeRules(nsfw_enabled=True, nsfw_source_ids='["special-source"]').for_source(row["gy_target_dir"])
            self.assertTrue(rules.nsfw_enabled)
            self.assertTrue(rules.nsfw_exclusive)
            repeated = asyncio.run(download_indexer_result(service, _RESULT_ID, "guangya", origin_namespace="agent", scope="agent"))
            self.assertTrue(repeated["duplicate"])
            self.assertEqual(repeated["request_id"], row["id"])
            backend.assert_called_once()








def test_candidate_refinement_reports_scope_without_confusing_subset_with_complete_snapshot():
    snapshot = _candidate_snapshot(count=50)
    for item, private in zip(snapshot['candidates'], snapshot['_private_items'], strict=True):
        item['title'] = private['title'] = f"Demo.1080p.{'SDR' if item['position'] == 50 else 'H264'}"
    ids = [item['result_id'] for item in snapshot['candidates']]
    result = select_candidates(snapshot, ids, {'dynamic_range': 'SDR'})
    assert result['result_ids'] == [ids[-1]]
    assert result['scope'] == {'candidate_count': 50, 'considered_count': 50, 'complete': True, 'pages': []}
    assert result['technical_filter']['unknown_excluded'] == 49
    assert _SECRET_MAGNET not in json.dumps(result) and _SECRET_TORRENT_URL not in json.dumps(result)
    partial = select_candidates(snapshot, ids[:12], {'dynamic_range': 'SDR'})
    assert partial['result_ids'] == []
    assert partial['scope'] == {'candidate_count': 50, 'considered_count': 12, 'complete': False, 'pages': []}
    cleared = select_candidates(snapshot, [], {'dynamic_range': 'SDR'})
    assert cleared['result_ids'] == []
