"""光鸭真盘响应契约：失败非空、游标翻页、任务终态与写后复核。"""
from __future__ import annotations

import json
import unittest
from copy import deepcopy
from unittest import mock

import httpx

from app.agent import guangya_recycle_actions as recycle
from app.agent import guangya_share_actions as shares
from app.agent import guangya_workspace_actions as workspace
from app.agent.errors import AgentToolError
from app.agent.models import ToolContext
from app.clients.guangya import (
    DirectoryEntryLimitError,
    GuangYaClient,
    IncompleteOfflineTaskListError,
    guangya_provider_task_state,
)
from tests.test_agent_guangya_sdk_capabilities import _RecycleClient, _ShareClient


class ReadClient(GuangYaClient):
    def __init__(self, payload):
        self._raw = mock.Mock()
        for name in ("fs_files", "fs_detail", "fs_recycle_files", "share_user_list", "cloud_task_list", "get_task_status"):
            getattr(self._raw, name).return_value = payload

    @property
    def raw(self):
        return self._raw


class GuangYaReadContractTests(unittest.TestCase):
    def test_failed_and_unknown_lists_are_not_empty_success(self):
        for payload in (
            None, {}, "unexpected", {"code": 500, "msg": "private-provider-error"},
            {"code": 0, "error": "private-token", "data": {"list": []}},
            {"data": {"code": 401, "list": []}}, {"data": None},
            {"data": {"total": 5}}, {"data": {"list": [None]}},
        ):
            for method in ("list_dir", "list_recycle", "list_user_shares", "list_offline_tasks"):
                with self.subTest(payload=payload, method=method):
                    with self.assertRaises(RuntimeError) as caught:
                        getattr(ReadClient(payload), method)()
                    self.assertNotIn("private-", str(caught.exception))

    def test_error_details_and_task_responses_are_not_missing_objects(self):
        for payload in (None, {}, {"code": 500}, {"error": "private-error"}):
            for method in ("file_info", "task_status"):
                with self.subTest(method=method, payload=payload), self.assertRaises(RuntimeError):
                    getattr(ReadClient(payload), method)("private-id")

    def test_file_absence_needs_a_successful_empty_detail_response(self):
        for payload in ({"data": {}}, {"data": "invalid"}, {"data": {"fileInfo": {}}}):
            with self.subTest(payload=payload), self.assertRaises(RuntimeError):
                ReadClient(payload).file_info("f")
        self.assertIsNone(ReadClient({"msg": "success", "data": {}}).file_info("f"))
        self.assertIsNone(ReadClient({"code": 0, "data": {"fileInfo": None}}).file_info("f"))

    def test_actual_empty_share_and_nested_list_shapes(self):
        self.assertEqual(GuangYaClient._extract_list({"msg": "success", "data": {}}), [])
        self.assertEqual(GuangYaClient._extract_list({"data": {"total": 0, "list": []}}), [])
        item = {"fileId": "f", "fileName": "sample", "resType": 2}
        for key in ("file_list", "fileList", "files", "list", "res_list"):
            with self.subTest(key=key):
                self.assertEqual(GuangYaClient._extract_list({"data": {key: [item]}}), [item])

    def test_short_pages_with_remaining_total_are_not_truncated(self):
        for method, sdk in (("list_dir", "fs_files"), ("list_recycle", "fs_recycle_files"), ("list_user_shares", "share_user_list")):
            client = ReadClient(None)
            records = [
                {"fileId": "1", "fileName": "one", "shareId": "share-1"},
                {"fileId": "2", "fileName": "two", "shareId": "share-2"},
            ]
            getattr(client.raw, sdk).side_effect = [
                {"msg": "success", "data": {"total": 2, "list": [row]}}
                for row in records
            ]
            with self.subTest(method=method):
                self.assertEqual(len(getattr(client, method)()), 2)
                self.assertEqual(getattr(client.raw, sdk).call_count, 2)

    def test_directory_page_size_tracks_the_read_budget(self):
        for limit, size in ((None, 1000), (0, 2), (1, 2), (17, 18), (999, 1000), (100000, 1000)):
            with self.subTest(limit=limit):
                client = ReadClient({"data": {"total": 0, "list": []}})
                self.assertEqual(list(client.iter_dir(max_items=limit)), [])
                self.assertEqual(client.raw.fs_files.call_args.kwargs["page_size"], size)

    def test_root_directory_keeps_zero_parent_for_entries_without_parent_field(self):
        client = ReadClient({"msg": "success", "data": {
            "total": 1, "list": [{"fileId": "root-file", "fileName": "root.mkv"}],
        }})

        entries = client.list_dir()

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].parent_id, "0")
        self.assertIsNone(client.raw.fs_files.call_args.kwargs["parent_id"])

    def test_large_directory_reads_complete_result_in_two_pages(self):
        rows = [{"fileId": str(i), "fileName": f"{i}.mkv"} for i in range(1201)]
        client = ReadClient(None)
        def files(*, parent_id, page, page_size):
            return {"data": {"total": len(rows), "list": rows[page * page_size:(page + 1) * page_size]}}
        client.raw.fs_files.side_effect = files
        self.assertEqual([f.file_id for f in client.list_dir()], [str(i) for i in range(1201)])
        self.assertEqual(client.raw.fs_files.call_count, 2)
        self.assertEqual([c.kwargs["page"] for c in client.raw.fs_files.call_args_list], [0, 1])

    def test_small_budget_requests_only_one_extra_item_for_overflow_detection(self):
        rows = [{"fileId": str(i), "fileName": f"{i}.mkv"} for i in range(50)]
        client = ReadClient(None)
        def files(*, parent_id, page, page_size):
            return {"data": {"total": len(rows), "list": rows[page * page_size:(page + 1) * page_size]}}
        client.raw.fs_files.side_effect = files
        with self.assertRaises(DirectoryEntryLimitError):
            list(client.iter_dir(max_items=2))
        self.assertEqual(client.raw.fs_files.call_count, 1)
        self.assertEqual(client.raw.fs_files.call_args.kwargs["page_size"], 3)

    def test_directory_pagination_accepts_stable_total_across_pages(self):
        client = ReadClient(None)
        client.raw.fs_files.side_effect = [
            {"data": {"total": 2, "list": [{"fileId": "a", "fileName": "A"}]}},
            {"data": {"total": 2, "list": [{"fileId": "b", "fileName": "B"}], "hasMore": False}},
        ]

        self.assertEqual([item.file_id for item in client.list_dir()], ["a", "b"])
        self.assertEqual(client.raw.fs_files.call_count, 2)

    def test_directory_pagination_rejects_changed_total(self):
        cases = (
            (
                {"total": 2, "list": [{"fileId": "a", "fileName": "A"}]},
                {"total": 3, "list": [{"fileId": "b", "fileName": "B"}]},
            ),
            (
                {"total": 3, "list": [{"fileId": "a", "fileName": "A"}, {"fileId": "b", "fileName": "B"}]},
                {"total": 2, "list": []},
            ),
        )
        for first, second in cases:
            client = ReadClient(None)
            client.raw.fs_files.side_effect = [
                {"data": first}, {"data": second},
            ]
            with self.subTest(first_total=first["total"], second_total=second["total"]):
                with self.assertRaisesRegex(RuntimeError, "分页总数发生变化") as caught:
                    client.list_dir()
                self.assertNotIn("private", str(caught.exception))
                self.assertEqual(client.raw.fs_files.call_count, 2)

    def test_directory_iterator_can_stop_before_unrelated_malformed_item(self):
        client = ReadClient({"data": {"total": 2, "list": [
            {"fileId": "wanted", "fileName": "Wanted.mkv", "size": 1},
            {"fileId": "other", "fileName": "Other.mkv", "size": "invalid"},
        ]}})
        items = client.iter_dir("source")
        self.assertEqual(next(items).file_id, "wanted")
        # 精准定位找到目标可立即结束；完整枚举仍必须暴露坏条目。
        with self.assertRaises(ValueError):
            next(items)
        self.assertEqual(client.raw.fs_files.call_count, 1)

    def test_directory_snapshot_budget_refusal_uses_one_authoritative_page(self):
        rows = [
            {"fileId": str(index), "fileName": f"{index}.mkv", "parentId": "0"}
            for index in range(1000)
        ]
        client = ReadClient(None)
        client.raw.fs_files.return_value = {"msg": "success", "data": {
            "total": 1001, "list": rows, "hasMore": True,
        }}

        self.assertIsNone(client.read_directory_snapshot(request_budget=3, max_items=2000))
        client.raw.fs_files.assert_called_once_with(parent_id="*", page=0, page_size=1000)

    def test_directory_snapshot_requires_two_equal_rounds_and_independent_roots(self):
        rows = [
            {"fileId": "a", "fileName": "A", "parentId": 0, "size": 10, "etag": "v1"},
            {"fileId": "b", "fileName": "B", "size": 20},
        ]
        client = ReadClient({"data": {"total": 2, "list": rows}})
        snapshot = client.read_directory_snapshot(request_budget=3, max_items=2)
        self.assertEqual(set(snapshot), {"a", "b"})
        self.assertTrue(all(file.parent_id == "0" for file in snapshot.values()))
        self.assertEqual([c.kwargs["parent_id"] for c in client.raw.fs_files.call_args_list], ["*", "*", None])

    def test_snapshot_root_pages_cannot_exceed_remaining_request_budget(self):
        rows = [{"fileId": str(i), "fileName": str(i), "parentId": "0"} for i in range(1001)]
        client = ReadClient(None)
        def files(*, parent_id, page, page_size):
            return {"data": {"total": len(rows), "list": rows[page * page_size:(page + 1) * page_size]}}
        client.raw.fs_files.side_effect = files
        with self.assertRaisesRegex(RuntimeError, "超过请求预算"):
            client.read_directory_snapshot(request_budget=5, max_items=2000)
        self.assertEqual(client.raw.fs_files.call_count, 5)
        client.raw.fs_files.reset_mock()
        self.assertEqual(len(client.read_directory_snapshot(request_budget=6, max_items=2000)), 1001)
        self.assertEqual(client.raw.fs_files.call_count, 6)

    def test_directory_snapshot_missing_parent_must_be_proven_by_root_listing(self):
        client = ReadClient(None)
        first = {"data": {"total": 1, "list": [{"fileId": "a", "fileName": "A"}]}}
        client.raw.fs_files.side_effect = [first, first, {"data": {"total": 0, "list": []}}]
        with self.assertRaisesRegex(RuntimeError, "根目录列表不一致"):
            client.read_directory_snapshot(request_budget=3, max_items=2)
        self.assertEqual(client.raw.fs_files.call_count, 3)

    def test_directory_snapshot_rejects_same_total_replacement_and_field_change(self):
        original = [
            {"fileId": "a", "fileName": "A", "parentId": "0", "size": 10},
            {"fileId": "b", "fileName": "B", "parentId": "0", "size": 20},
        ]
        replacements = (
            [
                {"fileId": "a", "fileName": "A", "parentId": "0", "size": 11},
                {"fileId": "b", "fileName": "B", "parentId": "0", "size": 20},
            ],
            [
                {"fileId": "a", "fileName": "A", "parentId": "0", "size": 10},
                {"fileId": "c", "fileName": "C", "parentId": "0", "size": 20},
            ],
        )
        for replacement in replacements:
            client = ReadClient(None)
            client.raw.fs_files.side_effect = [
                {"msg": "success", "data": {"total": 2, "list": deepcopy(original)}},
                {"msg": "success", "data": {"total": 2, "list": deepcopy(replacement)}},
            ]
            with self.subTest(replacement=replacement), self.assertRaisesRegex(RuntimeError, "两轮快照不一致"):
                client.read_directory_snapshot(request_budget=3, max_items=2)
            self.assertEqual(client.raw.fs_files.call_count, 2)

    def test_directory_snapshot_cancellation_is_an_error_not_a_partial_snapshot(self):
        before_request = ReadClient(None)
        with self.assertRaisesRegex(RuntimeError, "已取消"):
            before_request.read_directory_snapshot(request_budget=2, max_items=10, should_stop=lambda: True)
        before_request.raw.fs_files.assert_not_called()

        after_first_request = ReadClient(None)
        after_first_request.raw.fs_files.return_value = {"msg": "success", "data": {
            "total": 1, "list": [{"fileId": "a", "fileName": "A", "parentId": "0"}],
        }}
        with self.assertRaisesRegex(RuntimeError, "已取消"):
            after_first_request.read_directory_snapshot(
                request_budget=2,
                max_items=10,
                should_stop=lambda: after_first_request.raw.fs_files.call_count > 0,
            )
        after_first_request.raw.fs_files.assert_called_once()

    def test_directory_snapshot_rejects_invalid_identity_parent_and_total_contracts(self):
        cases = (
            ("duplicate id", {"total": 2, "list": [
                {"fileId": "a", "parentId": "0"}, {"fileId": "a", "parentId": "0"},
            ]}, "重复file_id"),
            ("missing id", {"total": 1, "list": [{"fileName": "A", "parentId": "0"}]}, "缺少file_id"),
            ("missing total", {"list": []}, "缺少有效total"),
        )
        for label, data, message in cases:
            client = ReadClient(None)
            client.raw.fs_files.return_value = {"msg": "success", "data": data}
            with self.subTest(label=label), self.assertRaisesRegex(RuntimeError, message):
                client.read_directory_snapshot(request_budget=10, max_items=10)
            self.assertEqual(client.raw.fs_files.call_count, 3 if label == "duplicate id" else 1)

    def test_directory_snapshot_rejects_changed_total_and_server_page_shrink(self):
        rows = [
            {"fileId": str(index), "fileName": str(index), "parentId": "0"}
            for index in range(1000)
        ]
        changed_total = ReadClient(None)
        changed_total.raw.fs_files.side_effect = [
            {"msg": "success", "data": {"total": 1001, "list": rows, "hasMore": True}},
            {"msg": "success", "data": {"total": 1002, "list": [{
                "fileId": "1000", "fileName": "1000", "parentId": "0",
            }], "hasMore": True}},
        ]
        with self.assertRaisesRegex(RuntimeError, "分页总数发生变化"):
            changed_total.read_directory_snapshot(request_budget=5, max_items=2000)
        self.assertEqual(changed_total.raw.fs_files.call_count, 2)

        shrunk = ReadClient(None)
        shrunk.raw.fs_files.return_value = {"msg": "success", "data": {
            "total": 1001, "list": rows[:500], "hasMore": True,
        }}
        with self.assertRaisesRegex(RuntimeError, "服务端缩页"):
            shrunk.read_directory_snapshot(request_budget=5, max_items=2000)
        # 缩页按协议错误处理，不降级成成本拒绝或继续超预算翻页。
        shrunk.raw.fs_files.assert_called_once()

    def test_unstable_snapshot_is_discarded_not_deduped_and_stable_rounds_are_required(self):
        a = {"fileId": "a", "fileName": "A", "parentId": "0"}
        b = {"fileId": "b", "fileName": "B", "parentId": "0"}
        bad = {"data": {"total": 2, "list": [a, a]}}
        good = {"data": {"total": 2, "list": [a, b]}}
        client = ReadClient(None)
        client.raw.fs_files.side_effect = [bad, good, good, good]
        result = client.read_directory_snapshot(request_budget=4, max_items=2)
        self.assertEqual(set(result), {"a", "b"})
        self.assertEqual(client.raw.fs_files.call_count, 4)
        self.assertEqual([c.kwargs["parent_id"] for c in client.raw.fs_files.call_args_list], ["*", "*", "*", None])

    def test_snapshot_retry_never_exceeds_original_budget_or_returns_partial_data(self):
        a = {"fileId": "a", "fileName": "A", "parentId": "0"}
        client = ReadClient(None)
        client.raw.fs_files.return_value = {"data": {"total": 2, "list": [a, a]}}
        with self.assertRaisesRegex(RuntimeError, "重复file_id"):
            client.read_directory_snapshot(request_budget=2, max_items=2)
        self.assertEqual(client.raw.fs_files.call_count, 1)

    def test_directory_terminal_page_without_total_checks_locked_count(self):
        enough = ReadClient(None)
        enough.raw.fs_files.side_effect = [
            {"data": {"total": 2, "list": [
                {"fileId": "a", "fileName": "A"}, {"fileId": "b", "fileName": "B"},
            ], "hasMore": True}},
            {"data": {"list": [], "hasMore": False}},
        ]
        self.assertEqual([item.file_id for item in enough.list_dir()], ["a", "b"])

        short = ReadClient(None)
        short.raw.fs_files.side_effect = [
            {"data": {"total": 3, "list": [
                {"fileId": "a", "fileName": "A"}, {"fileId": "b", "fileName": "B"},
            ], "hasMore": True}},
            {"data": {"list": [], "hasMore": False}},
        ]
        with self.assertRaisesRegex(RuntimeError, "条目数与总数不一致"):
            short.list_dir()
        self.assertEqual(short.raw.fs_files.call_count, 2)

    def test_directory_duplicate_ids_do_not_block_page_progress(self):
        client = ReadClient(None)
        client.raw.fs_files.side_effect = [
            {"data": {"total": 2, "list": [{"fileId": "a", "fileName": "A"}]}},
            {"data": {"total": 2, "list": [
                {"fileId": "a", "fileName": "A"}, {"fileId": "b", "fileName": "B"},
            ], "hasMore": False}},
        ]

        self.assertEqual([item.file_id for item in client.list_dir()], ["a", "b"])
        self.assertEqual(client.raw.fs_files.call_count, 2)

    def test_directory_total_ignores_bool_until_valid_total_appears(self):
        client = ReadClient(None)
        client.raw.fs_files.side_effect = [
            {"data": {"total": True, "list": [{"fileId": "a", "fileName": "A"}], "hasMore": True}},
            {"data": {"total": 2, "list": [{"fileId": "b", "fileName": "B"}], "hasMore": False}},
        ]

        self.assertEqual([item.file_id for item in client.list_dir()], ["a", "b"])

    def test_directory_cancellation_and_item_budget_keep_existing_behavior(self):
        cancelled = ReadClient(None)
        cancelled.raw.fs_files.return_value = {"data": {
            "total": 2, "list": [{"fileId": "a", "fileName": "A"}],
        }}
        rows = list(cancelled.iter_dir(should_stop=lambda: cancelled.raw.fs_files.call_count > 0))
        self.assertEqual([item.file_id for item in rows], ["a"])
        cancelled.raw.fs_files.assert_called_once()

        limited = ReadClient(None)
        limited.raw.fs_files.side_effect = [
            {"data": {"total": 3, "list": [
                {"fileId": "a", "fileName": "A"}, {"fileId": "b", "fileName": "B"},
            ]}},
            {"data": {"total": 3, "list": [{"fileId": "c", "fileName": "C"}]}},
        ]
        with self.assertRaises(DirectoryEntryLimitError):
            list(limited.iter_dir(max_items=2))
        self.assertEqual(limited.raw.fs_files.call_count, 2)

    def test_error_on_second_page_does_not_return_first_page_as_complete(self):
        for method, sdk in (("list_dir", "fs_files"), ("list_recycle", "fs_recycle_files"), ("list_user_shares", "share_user_list")):
            client = ReadClient(None)
            getattr(client.raw, sdk).side_effect = [
                {"data": {"total": 2, "list": [{"fileId": "1", "fileName": "a", "shareId": "s"}]}},
                {"code": 500},
            ]
            with self.subTest(method=method), self.assertRaises(RuntimeError):
                getattr(client, method)()

    def test_offline_cursor_protocol_instead_of_ignored_page_number(self):
        client = ReadClient({"msg": "success", "data": {
            "total": 2, "list": [{"taskId": "a", "fileName": "A", "status": 2}],
            "cursor": "private-cursor", "hasMore": True,
        }})
        client.raw.request.return_value = httpx.Response(200, json={"msg": "success", "data": {
            "total": 2, "list": [{"taskId": "b", "fileName": "B", "status": 2}],
            "cursor": "next-cursor", "hasMore": False,
        }})
        result = client.list_offline_tasks()
        self.assertEqual([x["id"] for x in result], ["a", "b"])
        client.raw.cloud_task_list.assert_called_once()
        request = client.raw.request.call_args
        self.assertEqual(request.kwargs["json"]["cursor"], "private-cursor")
        self.assertNotIn("page", request.kwargs["json"])

    def test_offline_cursor_end_omits_list_but_retains_total(self):
        client = ReadClient({"msg": "success", "data": {
            "total": 1, "list": [{"taskId": "a", "fileName": "A", "status": 2}],
            "cursor": "cursor", "hasMore": True,
        }})
        client.raw.request.return_value = httpx.Response(200, json={"msg": "success", "data": {
            "total": 1, "cursor": "cursor", "statusCounts": [],
        }})
        self.assertEqual(len(client.list_offline_tasks()), 1)
        client.raw.request.assert_called_once()
        client.raw.request.return_value = httpx.Response(200, json={"msg": "success", "data": {
            "total": 2, "cursor": "cursor", "statusCounts": [],
        }})
        with self.assertRaises(IncompleteOfflineTaskListError):
            client.list_offline_tasks()

    def test_offline_stuck_cursor_and_repeated_short_page_fail_closed(self):
        first = {"msg": "success", "data": {
            "total": 3, "hasMore": True, "cursor": "cursor",
            "list": [{"taskId": "a", "fileName": "A", "status": 2}],
        }}
        for second_id in ("a", "b"):
            client = ReadClient(first)
            second = deepcopy(first)
            second["data"]["list"][0]["taskId"] = second_id
            client.raw.request.return_value = httpx.Response(200, json=second)
            with self.subTest(second_id=second_id), self.assertRaises(IncompleteOfflineTaskListError):
                client.list_offline_tasks()

    def test_shared_file_cursor_and_budget_are_explicit(self):
        raw = mock.Mock()
        raw.share_access_token.return_value = {"data": {"accessToken": "private-token"}}
        raw.share_files_list.return_value = {"msg": "success", "data": {
            "list": [{"fileId": "f1", "fileName": "one"}], "total": 2, "cursor": "next",
        }}
        raw._public_post.return_value = {"msg": "success", "data": {
            "list": [{"fileId": "f2", "fileName": "two"}], "total": 2, "cursor": "end",
        }}
        with mock.patch("app.clients.guangya._load_raw", return_value=raw):
            result = ReadClient(None).list_share_files("https://www.guangyapan.com/s/public_test", page_size=10)
            self.assertEqual(result["count"], 2)
            self.assertNotIn("private-token", json.dumps(result))
            self.assertEqual(raw._public_post.call_args.args[1]["cursor"], "next")
            with self.assertRaisesRegex(RuntimeError, "分页上限"):
                ReadClient(None).list_share_files("https://www.guangyapan.com/s/public_test", page_size=10, max_pages=1)

    def test_full_share_list_rejects_legacy_full_page_at_budget(self):
        raw = mock.Mock()
        raw.share_access_token.return_value = {"data": {"accessToken": "private-token"}}
        raw.share_files_list.return_value = {"data": {"list": [
            {"fileId": "f1", "fileName": "one"},
            {"fileId": "f2", "fileName": "two"},
        ]}}
        with mock.patch("app.clients.guangya._load_raw", return_value=raw), self.assertRaisesRegex(
            RuntimeError, "分页上限",
        ):
            ReadClient(None).list_share_files(
                "https://www.guangyapan.com/s/public_test", page_size=2, max_pages=1,
            )
        raw.share_files_list.assert_called_once()
        raw._public_post.assert_not_called()

    def test_share_preview_keeps_one_page_when_more_files_exist(self):
        first = {"fileId": "f1", "fileName": "one"}
        cases = (
            {"list": [first], "total": 2, "cursor": "next"},
            {"list": [{"fileId": f"f{i}", "fileName": str(i)} for i in range(200)]},
        )
        for payload in cases:
            raw = mock.Mock()
            raw.share_access_token.return_value = {"data": {"accessToken": "private-token"}}
            raw.share_files_list.return_value = {"msg": "success", "data": payload}
            with self.subTest(count=len(payload["list"])), mock.patch(
                "app.clients.guangya._load_raw", return_value=raw,
            ):
                result = ReadClient(None).inspect_share("https://www.guangyapan.com/s/public_test")
                self.assertEqual(result["count"], len(payload["list"]))
                self.assertEqual(result["access_token"], "private-token")
                self.assertTrue(result["has_more"])
                raw.share_files_list.assert_called_once()
                raw._public_post.assert_not_called()

    def test_share_preview_marks_a_successful_short_page_as_final(self):
        raw = mock.Mock()
        raw.share_access_token.return_value = {"data": {"accessToken": "private-token"}}
        raw.share_files_list.return_value = {"msg": "success", "data": {"list": [
            {"fileId": "f1", "fileName": "one"},
        ]}}
        with mock.patch("app.clients.guangya._load_raw", return_value=raw):
            result = ReadClient(None).inspect_share("https://www.guangyapan.com/s/public_test")
        self.assertEqual(result["count"], 1)
        self.assertFalse(result["has_more"])
        raw.share_files_list.assert_called_once()
        raw._public_post.assert_not_called()

    def test_share_preview_does_not_hide_provider_or_item_errors(self):
        for response in ({"code": 500, "msg": "private-error"}, {"data": {"list": [None]}}):
            raw = mock.Mock()
            raw.share_access_token.return_value = {"data": {"accessToken": "private-token"}}
            raw.share_files_list.return_value = response
            with self.subTest(response=response), mock.patch(
                "app.clients.guangya._load_raw", return_value=raw,
            ), self.assertRaises(RuntimeError):
                ReadClient(None).inspect_share("https://www.guangyapan.com/s/public_test")
            raw._public_post.assert_not_called()


class GuangYaAgentReadCompletionTests(unittest.TestCase):
    def test_generic_provider_task_state_does_not_use_offline_task_codes(self):
        for payload, expected in (
            ({"status": 0}, "running"),
            ({"status": 1}, "running"),
            ({"status": 2, "detail": {"code": 0}}, "completed"),
            ({"status": 2, "detail": {"code": 157}}, "failed"),
            ({"status": 3}, "failed"),
            ({"status": "pending"}, "running"),
            ({"status": "unknown"}, "unknown"),
            ({"status": 4}, "unknown"),
            ({"status": 5}, "unknown"),
        ):
            with self.subTest(payload=payload):
                self.assertEqual(guangya_provider_task_state({"data": payload}), expected)

    def test_unknown_task_and_terminal_error_detail_are_not_success(self):
        client = _RecycleClient()
        for payload, expected, ok in (
            ({"status": "unknown"}, "unknown", False), ({}, "unknown", False),
            ({"status": 0}, "running", True), ({"status": 1}, "running", True),
            ({"status": 2, "detail": {"code": 0}}, "completed", True),
            ({"status": 2, "detail": {"code": 157, "msg": "private-error"}}, "failed", False),
            ({"status": 3}, "failed", False),
            ({"status": 4}, "unknown", False),
            ({"status": 5}, "unknown", False),
            ({"status": 99}, "unknown", False),
        ):
            with (
                self.subTest(payload=payload),
                mock.patch.object(recycle, "GuangYaClient", return_value=client),
                mock.patch.object(client, "task_status", return_value={"data": payload}),
            ):
                result = recycle.query_guangya_task_status({"guangya_task": {"task_id": "task", "operation": "copy"}}, ToolContext())
                self.assertEqual(result.status, expected)
                self.assertEqual(result.ok, ok)
                self.assertNotIn("private-error", json.dumps(result.to_dict()))
                if expected == "completed":
                    self.assertEqual(result.data["progress"], 1.0)

    def test_restore_and_clear_acceptance_survives_failed_verification(self):
        for operation in ("restore", "clear"):
            client = _RecycleClient()
            context = ToolContext(owner="owner", session_id="session")
            original = client.list_recycle

            def read_after_write(*, client=client, original=original, **kwargs):
                if not client.items:
                    raise RuntimeError("provider-private-error")
                return original(**kwargs)

            with mock.patch.object(recycle, "GuangYaClient", return_value=client):
                if operation == "restore":
                    listed = recycle.list_guangya_recycle({"page": 1, "page_size": 50}, context)
                    args = {"guangya_recycle_items": listed.references[0].value, "indices": [1]}
                    _preview, fingerprint = recycle.prepare_restore_guangya_recycle(args, context)
                    execute = recycle.execute_restore_guangya_recycle
                else:
                    args = {}
                    _preview, fingerprint = recycle.prepare_clear_guangya_recycle(args, context)
                    execute = recycle.execute_clear_guangya_recycle
                with mock.patch.object(client, "list_recycle", side_effect=read_after_write):
                    result = execute(args, fingerprint, context)
                self.assertEqual(result.status, "accepted")
                self.assertTrue(result.data["verification_pending"])
                self.assertFalse(result.data["verified"])
                self.assertEqual(result.references[0].kind, "guangya_task")
                self.assertNotIn("provider-private-error", json.dumps(result.to_dict()))

    def test_revoke_acceptance_survives_failed_verification(self):
        client = _ShareClient()
        context = ToolContext(owner="owner", session_id="session")
        original = client.list_user_shares

        def read_after_write(**kwargs):
            if not client.shares:
                raise RuntimeError("private-error")
            return original(**kwargs)

        with mock.patch.object(shares, "GuangYaClient", return_value=client):
            listed = shares.list_guangya_user_shares({"page": 1, "page_size": 50}, context)
            args = {"guangya_shares": listed.references[0].value, "indices": [1]}
            _preview, fingerprint = shares.prepare_revoke_guangya_shares(args, context)
            with mock.patch.object(client, "list_user_shares", side_effect=read_after_write):
                result = shares.execute_revoke_guangya_shares(args, fingerprint, context)
            self.assertEqual(result.status, "accepted")
            self.assertTrue(result.data["verification_pending"])
            self.assertFalse(result.data["verified"])

    def test_share_without_identity_is_not_silently_dropped(self):
        client = _ShareClient()
        client.shares = [{"title": "nameless-id"}]
        with (
            mock.patch.object(shares, "GuangYaClient", return_value=client),
            self.assertRaises(AgentToolError),
        ):
            shares.list_guangya_user_shares({"page": 1, "page_size": 50}, ToolContext())

    def test_model_keeps_type_size_extension_without_provider_identifiers(self):
        page = {
            "observation_ref": "OBS" + "A" * 32, "scope": "scope", "scopes": [],
            "page": 1, "total": 1, "has_more": False, "truncated": False,
            "entries": [{"object_ref": "opaque-object", "object_name": "episode.mkv",
                         "location": "scope", "kind": "video", "size": 456, "extension": "mkv"}],
        }
        with mock.patch.object(workspace, "_read_observation_page", return_value=page):
            result = workspace.query_guangya_filesystem({}, ToolContext())
        entry = result.model_data["entries"][0]
        self.assertEqual((entry["kind"], entry["size"], entry["extension"]), ("video", 456, "mkv"))
        self.assertNotIn("file_id", entry)


if __name__ == "__main__":
    unittest.main()
