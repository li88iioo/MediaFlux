from __future__ import annotations

from collections import deque
from typing import cast
from unittest import TestCase
from unittest.mock import patch

import httpx

from app.clients.guangya import GuangYaClient, GuangYaFile, GuangYaReadRejected, GuangYaWriteRejected
from app.modules.guangya_gcid import GCIDImportItemResult, GuangyaGCIDImporter

_API = "https://api.guangyapan.com/userres/v1/"
_TOKEN = "get_res_center_token"
_FLASH = "check_can_flash_upload"
_STATUS = "file/get_info_by_task_id"
_CANCEL = "file/delete_upload_task"
_GCID = "a" * 40
_MD5 = "b" * 32
_PENDING = {"code": 147, "msg": "文件上传中"}
_TASK = {"code": 0, "data": {"taskId": "task-1"}}
_ACK = {"msg": "success", "data": {}}
_MISS = {"code": 0, "data": {"canFlashUpload": False}}


class _Drive:
    def __init__(self):
        self.raw = self
        self.files = {"target": GuangYaFile("target", "导入目录", True)}
        self.replies = deque()
        self.calls = []
        self.created = []

    def reply(self, operation, data, *, status=200):
        self.replies.append((operation, data, status))

    def request(self, url, method, *, json):
        self.calls.append((url.removeprefix(_API), json))
        expected, data, status = self.replies.popleft()
        assert method == "POST" and url == _API + expected
        if callable(data):
            data = data()
        request = httpx.Request(method, url)
        if isinstance(data, str):
            return httpx.Response(status, text=data, request=request)
        return httpx.Response(status, json=data, request=request)

    def file_info(self, file_id):
        return self.files.get(file_id)

    def list_dir(self, parent_id):
        self.calls.append(("list_dir", parent_id))
        return [item for item in self.files.values() if item.parent_id == parent_id]

    def create_dir(self, name, parent_id):
        self.created.append((parent_id, name))
        directory_id = f"dir-{len(self.created)}"
        self.files[directory_id] = GuangYaFile(directory_id, name, True, parent_id=parent_id)
        return directory_id

    def landed(self, *, file_id="file-1", parent_id="target", name="样本.mkv", size=42, gcid=_GCID):
        self.files[file_id] = GuangYaFile(file_id, name, False, size, gcid, parent_id)
        return {"code": 0, "data": {"fileId": file_id}}


class GuangyaGCIDImporterTests(TestCase):
    def setUp(self):
        self.sleep = self.enterContext(patch("app.modules.guangya_gcid.sleep"))

    def _import(self, drive, **overrides):
        return self._import_with(GuangyaGCIDImporter(cast(GuangYaClient, drive)), **overrides)

    def _import_with(self, importer, **overrides):
        item = {"target_dir_id": "target", "path": "样本.mkv", "size": 42, "gcid": _GCID.upper()}
        return importer.import_file(**(item | overrides))

    def test_156_with_business_message_preserves_path_and_reimport_is_idempotent(self):
        drive = _Drive()
        drive.reply(_TOKEN, {"code": 156, "msg": "已命中资源，不能重复上传", "data": {"taskId": "task-1"}})
        drive.reply(_STATUS, lambda: drive.landed(parent_id="dir-2"))
        importer = GuangyaGCIDImporter(cast(GuangYaClient, drive))

        for _ in range(2):
            result = self._import_with(importer, path="剧集/第 1 季/样本.mkv", md5=_MD5.upper())
            self.assertEqual(result, GCIDImportItemResult(True, "file-1"))

        self.assertEqual(drive.created, [("target", "剧集"), ("dir-1", "第 1 季")])
        self.assertEqual([call for call in drive.calls if call[0] != "list_dir"], [
            (_TOKEN, {
                "capacity": 2, "name": "样本.mkv", "parentId": "dir-2",
                "res": {"fileSize": 42, "gcid": _GCID, "md5": _MD5},
            }),
            (_STATUS, {"taskId": "task-1"}),
        ])
        self.assertFalse(drive.replies)

    def test_import_rechecks_a_replaced_directory_only_after_a_confirmed_absence(self):
        drive = _Drive()
        importer = GuangyaGCIDImporter(cast(GuangYaClient, drive))
        drive.files["old"] = GuangYaFile("old", "Season 1", True, parent_id="target")
        drive.landed(file_id="ep1", parent_id="old")
        path = "Season 1/样本.mkv"
        self.assertEqual(self._import_with(importer, path=path), GCIDImportItemResult(True, "ep1"))
        del drive.files["old"]
        drive.files["new"] = GuangYaFile("new", "Season 1", True, parent_id="target")
        drive.landed(file_id="ep2", parent_id="new")
        lookup = drive.file_info
        for code, expected in (("101", GCIDImportItemResult(False, error_code="provider_rejected")),
                               ("149", GCIDImportItemResult(True, "ep2"))):
            def read(file_id):
                if file_id == "old":
                    raise GuangYaReadRejected("file_info", code=code)
                return lookup(file_id)

            with self.subTest(code=code), patch.object(drive, "file_info", side_effect=read):
                self.assertEqual(self._import_with(importer, path=path), expected)
        self.assertFalse(drive.created)
        self.assertTrue(all(operation == "list_dir" for operation, _ in drive.calls))

    def test_flash_probe_uses_full_resource_fields_and_result_task_id(self):
        drive = _Drive()
        drive.reply(_TOKEN, _TASK)
        drive.reply(_FLASH, {"code": 0, "data": {"canFlashUpload": True, "taskId": "result-task"}})
        drive.reply(_STATUS, _PENDING)
        drive.reply(_STATUS, lambda: drive.landed(parent_id="0"))

        result = self._import(drive, target_dir_id="0", md5=_MD5.upper())

        self.assertEqual(result, GCIDImportItemResult(True, "file-1"))
        self.assertEqual(drive.calls, [
            ("list_dir", "0"),
            (_TOKEN, {
                "capacity": 2, "name": "样本.mkv", "parentId": "",
                "res": {"fileSize": 42, "gcid": _GCID, "md5": _MD5},
            }),
            (_FLASH, {"taskId": "task-1", "fileSize": 42, "gcid": _GCID, "md5": _MD5}),
            (_STATUS, {"taskId": "result-task"}),
            (_STATUS, {"taskId": "result-task"}),
        ])
        self.sleep.assert_called_once_with(1)

    def test_negative_or_empty_flash_ack_cancels_only_after_empty_target_and_fresh_pending(self):
        for response in (_ACK, _MISS):
            with self.subTest(response=response):
                drive = _Drive()
                drive.reply(_TOKEN, _TASK)
                drive.reply(_FLASH, response)
                for _ in range(4):
                    drive.reply(_STATUS, _PENDING)
                drive.reply(_CANCEL, {"code": 0, "data": {"taskIds": ["task-1"]}})

                self.assertEqual(self._import(drive), GCIDImportItemResult(False, error_code="not_matched"))
                self.assertEqual(drive.calls[2:], [
                    (_FLASH, {"taskId": "task-1", "fileSize": 42, "gcid": _GCID}),
                    *[(_STATUS, {"taskId": "task-1"})] * 3,
                    ("list_dir", "target"),
                    (_STATUS, {"taskId": "task-1"}),
                    (_CANCEL, {"taskIds": ["task-1"]}),
                ])
                self.assertFalse(drive.replies)

    def test_completion_during_last_status_check_is_verified_without_cancellation(self):
        drive = _Drive()
        drive.reply(_TOKEN, _TASK)
        drive.reply(_FLASH, _ACK)
        for _ in range(3):
            drive.reply(_STATUS, _PENDING)
        drive.reply(_STATUS, drive.landed)

        self.assertEqual(self._import(drive), GCIDImportItemResult(True, "file-1"))
        self.assertNotIn(_CANCEL, [operation for operation, _ in drive.calls])
        self.assertFalse(drive.replies)

    def test_landed_target_without_result_file_id_is_verified_without_cancellation(self):
        drive = _Drive()
        drive.reply(_TOKEN, _TASK)
        drive.reply(_FLASH, _ACK)

        def landed_but_task_still_pending():
            drive.landed()
            return _PENDING

        drive.reply(_STATUS, landed_but_task_still_pending)
        drive.reply(_STATUS, _PENDING)
        drive.reply(_STATUS, _PENDING)

        self.assertEqual(self._import(drive), GCIDImportItemResult(True, "file-1"))
        self.assertEqual(drive.calls[-1], ("list_dir", "target"))
        self.assertFalse(drive.replies)

    def test_positive_foreign_or_unknown_task_evidence_does_not_authorize_cancellation(self):
        scenarios = (
            ({"code": 0, "data": {"canFlashUpload": True}}, [_PENDING] * 4),
            ({"code": 0, "canFlashUpload": True, "data": {}}, [_PENDING] * 4),
            ({"code": 0, "msg": "秒传成功", "data": {}}, [_PENDING] * 4),
            ({"code": 0, "data": {"canFlashUpload": False, "taskId": "other-task"}}, []),
            (_ACK, [{**_PENDING, "data": {"taskId": "other-task"}}]),
            (_ACK, [_ACK]),
        )
        for result, statuses in scenarios:
            with self.subTest(result=result, statuses=statuses):
                drive = _Drive()
                drive.reply(_TOKEN, _TASK)
                drive.reply(_FLASH, result)
                for status in statuses:
                    drive.reply(_STATUS, status)

                self.assertEqual(self._import(drive), GCIDImportItemResult(False, error_code="verify_failed"))
                self.assertNotIn(_CANCEL, [operation for operation, _ in drive.calls])
                self.assertFalse(drive.replies)

    def test_http_and_business_failures_and_missing_task_never_trigger_cleanup(self):
        for operation, status, body, code in (
            (_TOKEN, 429, "rate limit", "rate_limited"),
            (_TOKEN, 503, {"code": 156, "msg": "秒传成功"}, "request_failed"),
            (_TOKEN, 200, _ACK, "request_failed"),
            (_FLASH, 200, {"code": 127, "data": {"canFlashUpload": False}}, "rate_limited"),
            (_FLASH, 200, {"code": 0, "data": {"success": False, "canFlashUpload": False}}, "provider_rejected"),
            (_STATUS, 503, _PENDING, "request_failed"),
        ):
            with self.subTest(operation=operation, status=status, body=body):
                drive = _Drive()
                if operation != _TOKEN:
                    drive.reply(_TOKEN, _TASK)
                if operation == _STATUS:
                    drive.reply(_FLASH, _ACK)
                drive.reply(operation, body, status=status)

                self.assertEqual(self._import(drive), GCIDImportItemResult(False, error_code=code))
                self.assertEqual(drive.calls[-1][0], operation)
                self.assertFalse(drive.replies)

    def test_cleanup_failure_reports_unconfirmed_cancellation(self):
        drive = _Drive()
        drive.reply(_TOKEN, _TASK)
        drive.reply(_FLASH, _ACK)
        for _ in range(4):
            drive.reply(_STATUS, _PENDING)
        drive.reply(_CANCEL, {"code": 0}, status=503)

        self.assertEqual(self._import(drive), GCIDImportItemResult(False, error_code="cancel_failed"))
        self.assertEqual(drive.calls[-1], (_CANCEL, {"taskIds": ["task-1"]}))

    def test_existing_name_conflict_is_rejected_before_starting_upload(self):
        for existing in ({"size": 43}, {"gcid": "different"}):
            with self.subTest(existing=existing):
                drive = _Drive()
                drive.landed(**existing)

                self.assertEqual(self._import(drive), GCIDImportItemResult(False, error_code="name_conflict"))
                self.assertEqual(drive.calls, [("list_dir", "target")])

    def test_156_object_must_match_destination_name_size_and_gcid(self):
        for mismatch in ({"parent_id": "other"}, {"name": "other.mkv"}, {"size": 43}, {"gcid": "different"}):
            with self.subTest(mismatch=mismatch):
                drive = _Drive()

                def return_completed():
                    data = drive.landed(**mismatch)["data"]
                    return {"code": 156, "data": data}

                drive.reply(_TOKEN, return_completed)

                self.assertEqual(self._import(drive), GCIDImportItemResult(False, error_code="verify_failed"))
                self.assertEqual([operation for operation, _ in drive.calls], ["list_dir", _TOKEN])

    def test_raced_directory_creation_rereads_and_reuses_the_exact_directory(self):
        drive = _Drive()
        create = drive.create_dir

        def raced(name, parent_id):
            child_id = create(name, parent_id)
            drive.landed(parent_id=child_id)
            raise GuangYaWriteRejected("create_dir", code="159")

        with patch.object(drive, "create_dir", side_effect=raced):
            result = self._import(drive, path="剧集/样本.mkv")

        self.assertEqual(result, GCIDImportItemResult(True, "file-1"))
        self.assertEqual(drive.created, [("target", "剧集")])
        self.assertEqual(drive.calls, [("list_dir", "target"), ("list_dir", "target"), ("list_dir", "dir-1")])

    def test_invalid_path_and_target_fail_before_remote_writes(self):
        drive = _Drive()
        self.assertEqual(self._import(drive, path="../样本.mkv").error_code, "invalid_input")
        self.assertEqual(self._import(drive, target_dir_id="unknown").error_code, "invalid_target")
        self.assertFalse(drive.calls)
