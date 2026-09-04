"""光鸭 HTTP-200 业务失败必须被写操作识别。"""
from __future__ import annotations

import unittest

from app.clients.guangya import (
    GuangYaClient,
    GuangYaWriteRejected,
    _validate_write_response,
)


class _Raw:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def fs_rename(self, file_id, new_name):
        self.calls.append((file_id, new_name))
        return self.response


class _Client(GuangYaClient):
    def __init__(self, raw):
        self._test_raw = raw

    @property
    def raw(self):
        return self._test_raw


class GuangYaWriteResponseTests(unittest.TestCase):
    def test_business_code_166_is_rejected(self):
        with self.assertRaises(GuangYaWriteRejected) as caught:
            _validate_write_response(
                {"code": 166, "msg": "名称不可用，请更换后重试"},
                operation="rename",
            )
        self.assertEqual(caught.exception.code, "166")
        self.assertNotIn("名称不可用", str(caught.exception))

    def test_success_shapes_remain_compatible(self):
        for response in (
            None,
            {},
            {"msg": "success"},
            {"msg": "操作成功"},
            {"code": 0},
            {"success": True},
        ):
            with self.subTest(response=response):
                _validate_write_response(response, operation="rename")

    def test_nested_business_failure_is_not_hidden_by_transport_success(self):
        with self.assertRaises(GuangYaWriteRejected) as caught:
            _validate_write_response(
                {"code": 200, "msg": "success", "data": {"code": 166, "msg": "名称不可用"}},
                operation="rename",
            )
        self.assertEqual(caught.exception.code, "166")

    def test_negative_chinese_message_cannot_be_misread_as_success(self):
        for response in (
            {"msg": "操作未成功"},
            {"code": 200, "msg": "复制失败，请稍后重试"},
            {"code": 0, "message": "目标目录不可用"},
        ):
            with (
                self.subTest(response=response),
                self.assertRaises(GuangYaWriteRejected),
            ):
                _validate_write_response(response, operation="copy")

    def test_client_rename_validates_provider_payload(self):
        raw = _Raw({"code": 166, "msg": "名称不可用，请更换后重试"})
        client = _Client(raw)
        with self.assertRaises(GuangYaWriteRejected):
            client.rename("file-1", "changed.mkv")
        self.assertEqual(raw.calls, [("file-1", "changed.mkv")])

        raw.response = {"msg": "success"}
        self.assertTrue(client.rename("file-1", "changed.mkv"))


if __name__ == "__main__":
    unittest.main()
