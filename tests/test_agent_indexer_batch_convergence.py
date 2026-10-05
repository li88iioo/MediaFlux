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
from app.agent import indexer_actions
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
        self.enterContext(patch.object(indexer_actions.config, "get_bool", return_value=True))
        self.enterContext(patch.object(indexer_actions, "run_indexer_awaitable_sync", asyncio.run))

    def request_count(self):
        with closing(sqlite3.connect(self.db_path)) as connection:
            return connection.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0]

    def run_overlap(self, outcome, *, target="guangya", reverse=False, pending=False):
        """后端已认领 -> alias 返回 submitting -> 放行后端，完全不依赖 sleep。"""
        started = threading.Event()
        alias_returned = threading.Event()
        snapshots = {}
        resolved_ids = []
        if pending:
            existing = dispatcher.create_request(
                dispatcher.normalize_download_url(MAGNET), "", "", origin="agent:nyaa"
            )
            self.assertTrue(existing["created"])

        async def resolve(result_id, *, scope="manual"):
            resolved_ids.append(result_id)
            if result_id == ALIAS:
                self.assertTrue(await asyncio.to_thread(started.wait, 5), "实际后端未并发开始")
            return ResolvedDownload("magnet", MAGNET)

        service = SimpleNamespace(
            result_store=SimpleNamespace(get=lambda _: SimpleNamespace(site_id="nyaa", title="索引结果")),
            get_result=lambda _id, **_kwargs: SimpleNamespace(site_id="nyaa", title="索引结果"),
            resolve=resolve,
        )

        def remote_submit(row, **kwargs):
            self.assertEqual(db.get_download_request(row["id"])["status"], "submitting")
            started.set()
            self.assertTrue(alias_returned.wait(5), "重复项必须先返回；不得串行化")
            return dict(outcome)

        async def observe(*args, **kwargs):
            result = await download_indexer_result_public(*args, **kwargs)
            snapshots[result["result_id"]] = copy.deepcopy(result)
            if result["result_id"] == ALIAS:
                alias_returned.set()
            return result

        ids = [ALIAS, FIRST] if reverse else [FIRST, ALIAS]
        with (
            patch.object(dispatcher, "_submit_guangya", side_effect=remote_submit) as gy,
            patch.object(dispatcher, "_submit_qb", return_value={"ok": True, "task_id": "fake-qb"}) as qb,
            patch.object(indexer_actions, "download_indexer_result_public", side_effect=observe),
        ):
            result = indexer_actions._submit_resource_batch(
                {"result_ids": ids, "target": target}, service=service
            )
        self.assertCountEqual(resolved_ids, ids)
        self.assertEqual(gy.call_count, 1)
        self.assertEqual(qb.call_count, int(target == "both"))
        self.assertEqual(self.request_count(), 1)
        self.assertEqual(snapshots[ALIAS]["existing_status"], "submitting")
        self.assertTrue(snapshots[ALIAS]["duplicate"])
        self.assertEqual(snapshots[FIRST]["created"], not pending)
        self.assertEqual(snapshots[FIRST]["request_id"], snapshots[ALIAS]["request_id"])
        self.assertEqual([item["result_id"] for item in result.data["items"]], ids)
        items = {item["result_id"]: item for item in result.data["items"]}
        return result, items[FIRST], items[ALIAS]

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
        self.assertEqual(result.data["merged"], 1)
        self.assertEqual(result.data["duplicate"], 1)
        self.assertEqual(result.data["total"], 2)
        self.assertIn("合并", result.summary)

    def test_duplicate_failure_uses_final_submission_not_submitting(self):
        result, first, alias = self.run_overlap({"ok": False, "error": "synthetic-private-detail"})
        self.assertEqual(db.get_download_request(first["request_id"])["status"], "failed")
        self.assert_merged(result, first, alias, "failed")
        self.assertEqual(result.data["failed"], 1)
        self.assertEqual(result.data["succeeded"], 0)
        self.assertEqual(result.status, "unavailable")
        self.assertNotIn("synthetic-private-detail", str(result.to_dict()))

    def test_duplicate_success_is_one_accepted_task_not_partial_failure(self):
        result, first, alias = self.run_overlap({"ok": True, "task_id": "fake-gy"}, reverse=True)
        self.assert_merged(result, first, alias, "submitted")
        self.assertEqual(result.data["succeeded"], 1)
        self.assertEqual(result.status, "accepted")
        self.assertTrue(result.ok)
        self.assertEqual(result.error, "")

    def test_duplicate_partial_preserves_failed_backend_and_one_task_count(self):
        result, first, alias = self.run_overlap({"ok": False}, target="both")
        self.assert_merged(result, first, alias, "partial")
        self.assertEqual(alias["batch_result"]["succeeded"], ["qb"])
        self.assertEqual(alias["batch_result"]["failed"], ["guangya"])
        self.assertEqual(result.data["succeeded"], 1)
        self.assertEqual(result.status, "partial")
        self.assertIn("部分下载目标", result.error)

    def test_duplicate_unknown_outcome_stays_review_required(self):
        result, first, alias = self.run_overlap({"ok": False, "outcome_unknown": True})
        self.assert_merged(result, first, alias, "manual_review")
        self.assertEqual(result.status, "review_required")
        self.assertEqual(result.data["review_required"], 1)
        self.assertEqual(result.data["failed"], 0)
        self.assertIn("勿直接重复提交", result.error)
        self.assertFalse(alias["can_resubmit"])

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

        service = SimpleNamespace(
            result_store=SimpleNamespace(get=lambda _: SimpleNamespace(site_id="nyaa", title="索引结果")),
            get_result=lambda _id, **_kwargs: SimpleNamespace(site_id="nyaa", title="索引结果"), resolve=resolve
        )
        with patch.object(dispatcher, "_submit_guangya", return_value={"ok": True}) as remote:
            result = indexer_actions._submit_resource_batch(
                {"result_ids": [FIRST, ALIAS], "target": "guangya"}, service=service
            )
        first, alias = result.data["items"]
        self.assertEqual(remote.call_count, 1)
        self.assertEqual(self.request_count(), 2)
        self.assertNotEqual(first["request_id"], alias["request_id"])
        self.assertEqual(alias["existing_status"], "submitting")
        self.assertNotIn("batch_result", alias)
        self.assertEqual(result.data.get("merged", 0), 0)
        self.assertEqual(result.status, "partial")
        self.assertNotIn("other-owner", str(result.to_dict()))
        self.assertEqual(db.get_download_request(existing["id"])["status"], "submitting")

    def test_same_request_different_backends_are_not_merged(self):
        existing = dispatcher.create_request(
            dispatcher.normalize_download_url(MAGNET), "", "", origin="agent:nyaa"
        )
        self.assertTrue(db.claim_download_request(existing["id"], "guangya"))

        async def resolve(result_id, *, scope="manual"):
            return ResolvedDownload("magnet", MAGNET)

        service = SimpleNamespace(
            result_store=SimpleNamespace(get=lambda _: SimpleNamespace(site_id="nyaa", title="索引结果")),
            get_result=lambda _id, **_kwargs: SimpleNamespace(site_id="nyaa", title="索引结果"),
            resolve=resolve,
        )

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
            self.assertEqual(indexer_actions._converge_batch_duplicates(items), 0)
        self.assertEqual(items, before)
        self.assertEqual(db.get_download_request(existing["id"])["gy_status"], "submitting")

    def test_independent_downloads_still_submit_concurrently(self):
        remote_barrier = threading.Barrier(2)

        async def resolve(result_id, *, scope="manual"):
            return ResolvedDownload(
                "magnet", MAGNET if result_id == FIRST else MAGNET.replace("a" * 40, "b" * 40)
            )

        service = SimpleNamespace(
            result_store=SimpleNamespace(get=lambda _: SimpleNamespace(site_id="nyaa", title="索引结果")),
            get_result=lambda _id, **_kwargs: SimpleNamespace(site_id="nyaa", title="索引结果"),
            resolve=resolve,
        )

        def remote_submit(row, **kwargs):
            remote_barrier.wait(timeout=5)
            return {"ok": True}

        with patch.object(dispatcher, "_submit_guangya", side_effect=remote_submit) as remote:
            result = indexer_actions._submit_resource_batch(
                {"result_ids": [FIRST, ALIAS], "target": "guangya"}, service=service
            )
        self.assertEqual(remote.call_count, 2)
        self.assertEqual(self.request_count(), 2)
        self.assertEqual(result.status, "accepted")
        self.assertEqual(result.data["succeeded"], 2)
        self.assertEqual(result.data["merged"], 0)
        self.assertEqual(result.data["duplicate"], 0)


if __name__ == "__main__":
    unittest.main()
