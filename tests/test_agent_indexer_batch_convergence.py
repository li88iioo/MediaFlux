"""同批候选必须收敛到实际提交结果，而非幂等命中时的瞬时快照。"""
from __future__ import annotations

import asyncio
import copy
import sqlite3
import threading
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

from app import database as db
from app.indexers import downloads
from app.indexers.downloads import download_indexer_result_public
from app.indexers.models import ResolvedDownload
from app.modules import download_dispatcher as dispatcher
from tests.support import isolated_test_database

FIRST = "opaque-resource-first"
ALIAS = "opaque-resource-alias"
MAGNET = "magnet:?xt=urn:btih:" + "a" * 40


class BatchConvergenceTests(unittest.TestCase):
    def setUp(self):
        self.db_path = self.enterContext(isolated_test_database())
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("禁止外联")))
        self.enterContext(patch.object(downloads, "download_target_readiness",
                                      side_effect=lambda target: {name: True for name in ("qb", "guangya") if target in {name, "both"}}))

    @staticmethod
    def service(resolve):
        return SimpleNamespace(
            get_result=lambda _id, **_kwargs: SimpleNamespace(site_id="nyaa", site_name="Nyaa", title="索引结果", download_state="ready", download_kinds=("magnet",)),
            resolve=resolve,
        )

    @staticmethod
    def submit(service, ids, target, **kwargs):
        snapshot = downloads.preflight_indexer_download_batch(service, ids, target, scope="agent")
        return asyncio.run(downloads.submit_indexer_download_batch(
            service, ids, target, expected_snapshot=snapshot, origin_namespace="agent", scope="agent", **kwargs))

    def request_count(self):
        with closing(sqlite3.connect(self.db_path)) as connection:
            return connection.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0]

    def run_overlap(self, outcome, *, target="guangya", reverse=False, pending=False, pending_origin="agent:nyaa", verification_targets=None):
        """先解析整批；后端认领后 alias 返回 submitting，再放行后端。"""
        started = threading.Event()
        alias_returned = threading.Event()
        snapshots = {}
        resolved_ids = []
        persisted_before_write = []
        if pending:
            existing = dispatcher.create_request(
                dispatcher.normalize_download_url(MAGNET), "", "", origin=pending_origin
            )
            self.assertTrue(existing["created"])

        async def resolve(result_id, *, scope="manual"):
            resolved_ids.append(result_id)
            return ResolvedDownload("magnet", MAGNET)

        service = self.service(resolve)

        def remote_submit(row, **kwargs):
            self.assertCountEqual(resolved_ids, [FIRST, ALIAS], "写入前必须完成整批只读解析")
            get_download_request_result = db.get_download_request(row["id"])
            assert get_download_request_result is not None
            self.assertEqual(get_download_request_result["status"], "submitting")
            if verification_targets:
                saved = db.get_agent_download_verification(row["id"])
                self.assertIsNotNone(saved, "远端写入前必须已持久保存复核意图")
                self.assertEqual(saved["owner"], "batch-owner")
                persisted_before_write.extend(saved["targets"])
            started.set()
            self.assertTrue(alias_returned.wait(5), "重复项必须先返回；不得串行化")
            return dict(outcome)

        submit_resolved = downloads._submit_indexer_download
        async def observe(service, resolved, **kwargs):
            if resolved.snapshot["result_id"] == ALIAS:
                self.assertTrue(await asyncio.to_thread(started.wait, 5), "实际后端未并发开始")
            result = await submit_resolved(service, resolved, **kwargs)
            snapshots[result["result_id"]] = copy.deepcopy(result)
            if result["result_id"] == ALIAS:
                alias_returned.set()
            return result

        ids = [ALIAS, FIRST] if reverse else [FIRST, ALIAS]
        with (
            patch.object(dispatcher, "_submit_guangya", side_effect=remote_submit) as gy,
            patch.object(dispatcher, "_submit_qb", return_value={"ok": True, "task_id": "fake-qb"}) as qb,
            patch.object(downloads, "_submit_indexer_download", side_effect=observe),
        ):
            result = self.submit(service, ids, target, verification_targets=verification_targets,
                                 verification_owner="batch-owner" if verification_targets else "")
        self.assertCountEqual(resolved_ids, ids)
        self.assertEqual(gy.call_count, 1)
        self.assertEqual(qb.call_count, int(target == "both"))
        self.assertEqual(self.request_count(), 1)
        self.assertEqual(snapshots[ALIAS]["existing_status"], "submitting")
        self.assertTrue(snapshots[ALIAS]["duplicate"])
        self.assertEqual(snapshots[FIRST]["created"], not pending)
        self.assertEqual(snapshots[FIRST]["request_id"], snapshots[ALIAS]["request_id"])
        self.assertEqual([item["result_id"] for item in result["items"]], ids)
        if verification_targets:
            expected = {item["episode"] for targets in verification_targets.values() for item in targets}
            self.assertEqual({item["episode"] for item in persisted_before_write}, expected)
            self.assertEqual([item["library_verification"]["target_count"] for item in result["items"]], [len(expected)] * len(ids))
        items = {item["result_id"]: item for item in result["items"]}
        return result, items[FIRST], items[ALIAS]

    def test_same_pack_aliases_persist_all_targets_before_dispatch_and_survive_restart(self):
        proof = {"title": "目标剧集", "tmdb_id": "123", "season": 1, "episode": 2,
                 "as_of": "2026-08-03", "library_name": ""}
        for pending, origin in ((False, "agent:nyaa"), (True, "agent:nyaa"), (True, "indexer:nyaa")):
            with self.subTest(recover_pending=pending, origin=origin), isolated_test_database():
                # request_count 的连接必须指向本次副本。
                old_path, self.db_path = self.db_path, db.resolve_db_path()
                try:
                    result, first, alias = self.run_overlap({"ok": True}, pending=pending, pending_origin=origin, verification_targets={
                        FIRST: [proof], ALIAS: [{**proof, "episode": 3}],
                    })
                    self.assert_merged(result, first, alias, "submitted")
                    db.init_db()
                    saved = db.get_agent_download_verification(first["request_id"])
                    assert saved is not None
                    self.assertEqual({item["episode"] for item in saved["targets"]}, {2, 3})
                    self.assertEqual(saved["owner"], "batch-owner")
                finally:
                    self.db_path = old_path

    def test_verification_registration_failure_rolls_back_new_request_before_remote_write(self):
        proof = {"title": "目标剧集", "tmdb_id": "123", "season": 1, "episode": 2,
                 "as_of": "2026-08-03", "library_name": ""}
        with patch("app.repositories.agent_download_verification.enqueue_agent_download_verification",
                   side_effect=sqlite3.OperationalError("injected storage failure")), \
             patch.object(downloads, "dispatch_request") as backend:
            with self.assertRaises(sqlite3.OperationalError):
                downloads.submit_download_input(dispatcher.normalize_download_url(MAGNET), "qb",
                    origin="agent:nyaa", verification_targets=[proof], verification_owner="batch-owner")
        backend.assert_not_called()
        self.assertEqual(self.request_count(), 0)

    def test_existing_hybrid_identity_merges_disjoint_magnet_keys_before_write(self):
        import hashlib
        from tests.test_qbittorrent_523_compat import _bencode

        info = {b"file tree": {}, b"meta version": 2, b"name": b"hybrid",
                b"piece length": 16384, b"pieces": b"x" * 20}
        item = dispatcher.torrent_download_input("hybrid.torrent", _bencode({b"info": info}))
        existing = dispatcher.create_request(item, "", "", origin="indexer:nyaa")
        v2 = "magnet:?xt=urn:btmh:1220" + hashlib.sha256(_bencode(info)).hexdigest()
        async def resolve(result_id, *, scope):
            return ResolvedDownload("magnet", item.source_value if result_id == FIRST else v2)
        proof = {"title": "目标剧集", "tmdb_id": "123", "season": 1, "episode": 2,
                 "as_of": "2026-08-03", "library_name": ""}
        def remote(row, **kwargs):
            self.assertEqual(row["id"], existing["id"])
            saved = db.get_agent_download_verification(row["id"])
            assert saved is not None
            self.assertEqual({target["episode"] for target in saved["targets"]}, {2, 3})
            return {"ok": True, "task_id": "hybrid-qb"}
        with patch.object(dispatcher, "_submit_qb", side_effect=remote) as backend:
            result = self.submit(self.service(resolve), [FIRST, ALIAS], "qb",
                verification_targets={FIRST: [proof], ALIAS: [{**proof, "episode": 3}]}, verification_owner="batch-owner")
        backend.assert_called_once()
        self.assertEqual(self.request_count(), 1)
        self.assertEqual([receipt["library_verification"]["target_count"] for receipt in result["items"]], [2, 2])

    def test_failed_registration_cannot_submit_reused_pending_or_append_backend(self):
        proof = {"title": "目标剧集", "tmdb_id": "123", "season": 1, "episode": 2,
                 "as_of": "2026-08-03", "library_name": ""}
        item = dispatcher.normalize_download_url(MAGNET)
        existing = dispatcher.create_request(item, "", "", origin="agent:nyaa")
        for phase in ("pending", "submitted"):
            with self.subTest(phase=phase):
                db.update_download_request(existing["id"], status=phase, targets="qb", qb_status=phase)
                with patch.object(db, "enqueue_agent_download_verification", return_value="not_eligible"), \
                     patch.object(dispatcher, "_submit_qb") as qb, patch.object(dispatcher, "_submit_guangya") as gy:
                    with self.assertRaises(downloads.IndexerValidationError):
                        downloads.submit_download_input(item, "both", origin="agent:nyaa",
                            verification_targets=[proof], verification_owner="batch-owner")
                qb.assert_not_called()
                gy.assert_not_called()

    def test_process_exit_after_remote_acceptance_cannot_lose_targets_or_repeat_download(self):
        class ProcessExit(BaseException):
            pass
        proof = {"title": "目标剧集", "tmdb_id": "123", "season": 1, "episode": 2,
                 "as_of": "2026-08-03", "library_name": ""}
        async def resolve(result_id, *, scope):
            return ResolvedDownload("magnet", MAGNET)
        service = self.service(resolve)
        targets = {FIRST: [proof], ALIAS: [{**proof, "episode": 3}]}
        request_ids = []
        def remote(row, **kwargs):
            request_ids.append(row["id"])
            saved = db.get_agent_download_verification(row["id"])
            assert saved is not None
            self.assertEqual({target["episode"] for target in saved["targets"]}, {2, 3})
            raise ProcessExit("remote accepted before local receipt")
        with patch.object(dispatcher, "_submit_qb", side_effect=remote) as backend:
            with self.assertRaises(ProcessExit):
                self.submit(service, [FIRST, ALIAS], "qb", verification_targets=targets, verification_owner="batch-owner")
            db.init_db()
            saved = db.get_agent_download_verification(request_ids[0])
            assert saved is not None
            self.assertEqual({target["episode"] for target in saved["targets"]}, {2, 3})
            replay = self.submit(service, [FIRST, ALIAS], "qb", verification_targets=targets, verification_owner="batch-owner")
        backend.assert_called_once()
        self.assertTrue(all(item["duplicate"] for item in replay["items"]))
        self.assertEqual({item["request_id"] for item in replay["items"]}, {request_ids[0]})
        self.assertEqual(self.request_count(), 1)

    def assert_merged(self, result, first, alias, status):
        self.assertEqual(first["status"], status)
        self.assertEqual(alias["existing_status"], status)
        self.assertEqual(alias["status"], "duplicate")
        self.assertTrue(alias["duplicate"])
        self.assertFalse(alias["created"])
        self.assertFalse(alias["ok"], "合并不是另一次写成功")
        self.assertEqual(alias["succeeded"], [])
        self.assertEqual(alias["batch_result"], {
            "result_id": FIRST,
            "status": status,
            "succeeded": first["succeeded"],
            "failed": first["failed"],
            "error": first["error"],
        })
        self.assertIn("同批", alias["error"])
        self.assertNotIn("正在处理", alias["error"])
        self.assertEqual(result["merged"], 1)
        self.assertEqual(result["duplicate"], 1)
        self.assertEqual(result["total"], 2)
        self.assertIn("合并", result["summary"])

    def test_duplicate_failure_uses_final_submission_not_submitting(self):
        result, first, alias = self.run_overlap({"ok": False, "error": "synthetic-private-detail"})
        get_download_request_result_2 = db.get_download_request(first["request_id"])
        assert get_download_request_result_2 is not None
        self.assertEqual(get_download_request_result_2["status"], "failed")
        self.assert_merged(result, first, alias, "failed")
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["succeeded"], 0)
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("synthetic-private-detail", str(result))

    def test_duplicate_success_is_one_accepted_task_not_partial_failure(self):
        result, first, alias = self.run_overlap({"ok": True, "task_id": "fake-gy"}, reverse=True)
        self.assert_merged(result, first, alias, "submitted")
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["status"], "accepted")
        self.assertTrue(result["ok"])
        self.assertEqual(result["error"], "")

    def test_duplicate_partial_preserves_failed_backend_and_one_task_count(self):
        result, first, alias = self.run_overlap({"ok": False}, target="both")
        self.assert_merged(result, first, alias, "partial")
        self.assertEqual(alias["batch_result"]["succeeded"], ["qb"])
        self.assertEqual(alias["batch_result"]["failed"], ["guangya"])
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["status"], "partial")
        assert result["error"] is not None
        self.assertIn("部分下载目标", result["error"])

    def test_duplicate_unknown_outcome_stays_review_required(self):
        result, first, alias = self.run_overlap({"ok": False, "outcome_unknown": True})
        self.assert_merged(result, first, alias, "manual_review")
        self.assertEqual(result["status"], "review_required")
        self.assertEqual(result["review_required"], 1)
        self.assertEqual(result["failed"], 0)
        assert result["error"] is not None
        self.assertIn("勿直接重复提交", result["error"])
        self.assertFalse(alias["can_resubmit"])
        original = db.get_download_request(first["request_id"])
        assert original is not None
        self.assertEqual(original["gy_status"], "outcome_unknown")
        async def resolve(_result_id, *, scope):
            return ResolvedDownload("magnet", MAGNET)
        with patch.object(dispatcher, "_submit_guangya", side_effect=AssertionError("未知结果不能自动重投")) as remote:
            repeated = self.submit(self.service(resolve), [FIRST, ALIAS], "guangya")
        remote.assert_not_called()
        self.assertEqual([item["existing_status"] for item in repeated["items"]], [original["status"]] * 2)
        self.assertEqual((repeated["status"], repeated["succeeded"]), ("conflict", 0))
        self.assertEqual(db.get_download_request(first["request_id"]), original)
        self.assertEqual(self.request_count(), 1)

    def test_pending_request_reuse_can_supply_final_result_without_created(self):
        result, first, alias = self.run_overlap({"ok": True}, pending=True)
        self.assertFalse(first["created"])
        self.assert_merged(result, first, alias, "submitted")

    def test_external_in_progress_is_not_merged_with_different_request(self):
        existing = dispatcher.create_request(
            dispatcher.normalize_download_url(MAGNET), "other-chat", "", user_id="other-owner"
        )
        self.assertTrue(db.claim_download_request(existing["id"], "guangya"))

        async def resolve(result_id, *, scope="manual"):
            return ResolvedDownload("magnet", MAGNET if result_id == ALIAS else MAGNET.replace("a" * 40, "b" * 40))

        service = self.service(resolve)
        with patch.object(dispatcher, "_submit_guangya", return_value={"ok": True}) as remote:
            result = self.submit(service, [FIRST, ALIAS], "guangya")
        first, alias = result["items"]
        self.assertEqual(remote.call_count, 1)
        self.assertEqual(self.request_count(), 2)
        self.assertNotEqual(first["request_id"], alias["request_id"])
        self.assertEqual(alias["existing_status"], "submitting")
        self.assertNotIn("batch_result", alias)
        self.assertEqual(result.get("merged", 0), 0)
        self.assertEqual(result["status"], "partial")
        self.assertNotIn("other-owner", str(result))
        get_download_request_result_3 = db.get_download_request(existing["id"])
        assert get_download_request_result_3 is not None
        self.assertEqual(get_download_request_result_3["status"], "submitting")

    def test_same_request_different_backends_are_not_merged(self):
        existing = dispatcher.create_request(
            dispatcher.normalize_download_url(MAGNET), "", "", origin="agent:nyaa"
        )
        self.assertTrue(db.claim_download_request(existing["id"], "guangya"))

        async def resolve(result_id, *, scope="manual"):
            return ResolvedDownload("magnet", MAGNET)

        service = self.service(resolve)

        async def submit():
            return list(await asyncio.gather(
                download_indexer_result_public(service, FIRST, "qb", origin_namespace="agent"),
                download_indexer_result_public(service, ALIAS, "guangya", origin_namespace="agent"),
            ))

        with (
            patch.object(dispatcher, "_submit_qb", return_value={"ok": True}) as qb,
            patch.object(dispatcher, "_submit_guangya", side_effect=AssertionError("不得重提光鸭")) as gy,
        ):
            items = asyncio.run(submit())
        self.assertEqual(qb.call_count, 1)
        self.assertEqual(gy.call_count, 0)
        self.assertEqual(self.request_count(), 1)
        self.assertEqual(items[0]["request_id"], items[1]["request_id"])
        self.assertEqual(items[0]["status"], "submitted")
        self.assertEqual(items[1]["status"], "duplicate")
        before = copy.deepcopy(items)
        # 已有真实分发结果后，反馈收敛不得再查询其它 owner 或全局请求状态。
        with patch.object(db, "get_download_request", side_effect=AssertionError("不得查询全局任务")):
            self.assertEqual(downloads._converge_batch_duplicates(items), 0)
        self.assertEqual(items, before)
        get_download_request_result_4 = db.get_download_request(existing["id"])
        assert get_download_request_result_4 is not None
        self.assertEqual(get_download_request_result_4["gy_status"], "submitting")

    def test_independent_downloads_still_submit_concurrently(self):
        remote_barrier = threading.Barrier(2)

        async def resolve(result_id, *, scope="manual"):
            return ResolvedDownload(
                "magnet", MAGNET if result_id == FIRST else MAGNET.replace("a" * 40, "b" * 40)
            )

        service = self.service(resolve)

        def remote_submit(row, **kwargs):
            remote_barrier.wait(timeout=5)
            return {"ok": True}

        with patch.object(dispatcher, "_submit_guangya", side_effect=remote_submit) as remote:
            result = self.submit(service, [FIRST, ALIAS], "guangya")
            repeated = self.submit(service, [FIRST, ALIAS], "guangya")
        self.assertEqual(remote.call_count, 2)
        self.assertEqual(self.request_count(), 2)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["succeeded"], 2)
        self.assertEqual(result["merged"], 0)
        self.assertEqual(result["duplicate"], 0)
        self.assertEqual(repeated["duplicate"], 2)
        self.assertEqual(repeated["status"], "conflict")

    def test_batch_rechecks_all_before_start_and_each_after_resolution(self):
        for phase in ("before", "during"):
            with self.subTest(phase=phase):
                changed = False
                resolved = []

                async def resolve(result_id, *, scope):
                    nonlocal changed
                    resolved.append(result_id)
                    changed |= result_id == ALIAS
                    return ResolvedDownload("magnet", MAGNET)

                service = self.service(resolve)
                original = service.get_result
                def get_result(result_id, **kwargs):
                    item = original(result_id, **kwargs)
                    if changed and result_id == ALIAS:
                        item.title = "changed candidate"
                    return item
                service.get_result = get_result
                snapshot = downloads.preflight_indexer_download_batch(service, [FIRST, ALIAS], "guangya", scope="agent")
                self.assertEqual(self.request_count(), 0)
                changed = phase == "before"
                with patch.object(dispatcher, "_submit_guangya", return_value={"ok": True}) as remote:
                    pending = downloads.submit_indexer_download_batch(service, [FIRST, ALIAS], "guangya",
                        expected_snapshot=snapshot, origin_namespace="agent", scope="agent")
                    if phase == "before":
                        with self.assertRaises(downloads.DownloadSnapshotChanged):
                            asyncio.run(pending)
                        self.assertEqual(resolved, [])
                        remote.assert_not_called()
                    else:
                        result = asyncio.run(pending)
                        self.assertEqual((result["status"], result["succeeded"], result["failed"]), ("partial", 1, 1))
                        self.assertTrue(result["items"][1]["pre_submission_failure"])
                        self.assertEqual(remote.call_count, 1)

    def test_later_resolution_config_change_also_invalidates_earlier_resolved_item(self):
        from app import config

        values = {"TG_QB_SAVE_PATH": "/frozen"}
        original_get = config.get
        async def resolve(result_id, *, scope):
            if result_id == ALIAS:
                values["TG_QB_SAVE_PATH"] = "/changed"
            return ResolvedDownload("magnet", MAGNET)
        with patch.object(config, "get", side_effect=lambda key, default="": values.get(key, original_get(key, default))), \
             patch.object(dispatcher, "_submit_qb") as backend:
            result = self.submit(self.service(resolve), [FIRST, ALIAS], "qb")
        backend.assert_not_called()
        self.assertEqual(self.request_count(), 0)
        self.assertTrue(all(item["pre_submission_failure"] for item in result["items"]))

    def test_cancelled_batch_does_not_start_queued_items_or_replay_started_write(self):
        entered, release = threading.Event(), threading.Event()
        resolved = []

        async def resolve(result_id, *, scope):
            resolved.append(result_id)
            return ResolvedDownload("magnet", MAGNET)

        def remote_submit(row, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5), "测试必须释放已开始的提交")
            return {"ok": True}

        service = self.service(resolve)
        snapshot = downloads.preflight_indexer_download_batch(service, [FIRST, ALIAS], "guangya", scope="agent")
        async def cancel():
            task = asyncio.create_task(downloads.submit_indexer_download_batch(service, [FIRST, ALIAS], "guangya",
                expected_snapshot=snapshot, origin_namespace="agent", scope="agent"))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()

        with patch.object(downloads, "_DOWNLOAD_LIMIT", 1), patch.object(dispatcher, "_submit_guangya", side_effect=remote_submit) as remote:
            asyncio.run(cancel())
            self.assertCountEqual(resolved, [FIRST, ALIAS])
            repeated = self.submit(service, [FIRST], "guangya")
        self.assertEqual(remote.call_count, 1)
        self.assertEqual(self.request_count(), 1)
        self.assertEqual(repeated["items"][0]["existing_status"], "submitted")

    def test_cancel_during_resolution_never_creates_requests(self):
        async def check():
            entered = asyncio.Event()
            async def resolve(result_id, *, scope):
                entered.set()
                await asyncio.Future()
            service = self.service(resolve)
            snapshot = downloads.preflight_indexer_download_batch(service, [FIRST, ALIAS], "qb", scope="agent")
            task = asyncio.create_task(downloads.submit_indexer_download_batch(service, [FIRST, ALIAS], "qb",
                expected_snapshot=snapshot, origin_namespace="agent", scope="agent"))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        with patch.object(downloads, "dispatch_request") as backend:
            asyncio.run(check())
        backend.assert_not_called()
        self.assertEqual(self.request_count(), 0)


if __name__ == "__main__":
    unittest.main()
