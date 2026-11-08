"""分享公共客户端冻结真实对象，写前世代校验与写后受理语义保持。"""
from __future__ import annotations

from copy import deepcopy
from typing import Any
from unittest.mock import Mock

import pytest

from app.clients.guangya import GuangYaClient, GuangYaFile, GuangYaShareError


class ShareClient(GuangYaClient):
    logged_in = True
    credential_generation = 9

    def __init__(self, generation=9):
        self.credential_generation = generation
        self.files = [GuangYaFile("file-1", "测试目录", True, parent_id="0", etag="etag-1")]
        self.shares = [{"id": str(20001 + i), "shareId": f"public-link-{i}", "title": "同名分享",
                        "status": "active", "updateTime": "t1", "fileCount": 1} for i in range(2)]
        self.writes = []
        self.closed = 0
        self.create_response: dict[str, Any] = {"code": 200, "data": {"shareId": "created-share", "code": "AB12"}}
        self.read_failure = False

    def file_info(self, file_id):
        return deepcopy(next((item for item in self.files if item.file_id == file_id), None))

    def list_user_shares(self, **kwargs):
        if self.read_failure:
            raise RuntimeError("private-provider-error")
        return deepcopy(self.shares)

    def create_user_share(self, file_ids, **kwargs):
        self.writes.append(("create", file_ids, kwargs))
        return deepcopy(self.create_response)

    def delete_user_shares(self, share_ids):
        self.writes.append(("revoke", share_ids))
        self.shares = [item for item in self.shares if item["id"] not in share_ids]
        return True

    def close(self):
        self.closed += 1
        return True


@pytest.mark.parametrize("generation", [0, 9])
@pytest.mark.parametrize("revoke", [False, True])
def test_same_generation_including_zero_uses_only_frozen_ids(generation, revoke):
    client = ShareClient(generation)
    snapshot = client.prepare_user_share_revoke(["20002"]) if revoke else client.prepare_user_share(["file-1"])
    if revoke:
        client.shares.reverse()
    result = client.submit_prepared_user_share(snapshot, revoke=revoke)
    assert result["status"] == "completed" and result["verified"]
    assert client.writes[0][1] == (["20002"] if revoke else ["file-1"])
    assert "credential_generation" not in result


@pytest.mark.parametrize("revoke", [False, True])
@pytest.mark.parametrize("point", ["before_read", "during_read", "before_write"])
def test_generation_changes_fail_before_provider_write(revoke, point):
    client = ShareClient()
    snapshot = client.prepare_user_share_revoke(["20001"]) if revoke else client.prepare_user_share(["file-1"])
    if point == "before_read":
        client.credential_generation += 1
    elif point == "during_read":
        method = "list_user_shares" if revoke else "file_info"
        original = getattr(client, method)
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            client.credential_generation += 1
            return result
        setattr(client, method, changed)
    def cancel_check():
        if point == "before_write":
            client.credential_generation += 1
    with pytest.raises(GuangYaShareError) as error:
        client.submit_prepared_user_share(snapshot, revoke=revoke, cancel_check=cancel_check)
    assert error.value.code == "confirmation_stale" and client.writes == []


@pytest.mark.parametrize("revoke", [False, True])
def test_accepted_is_retained_without_verifiable_result(revoke):
    client = ShareClient()
    snapshot = client.prepare_user_share_revoke(["20001"]) if revoke else client.prepare_user_share(["file-1"])
    if revoke:
        original = client.delete_user_shares
        def accepted(share_ids):
            original(share_ids)
            client.read_failure = True
            return True
        client.delete_user_shares = accepted
    else:
        client.create_response = {"code": 200, "msg": "操作成功"}
    result = client.submit_prepared_user_share(snapshot, revoke=revoke)
    assert result["status"] == "accepted" and result["verification_pending"] and not result["verified"]
    assert len(client.writes) == 1 and "private-provider-error" not in str(result)


@pytest.mark.parametrize("arguments", [
    {"file_ids": []}, {"file_ids": ["file-1"] * 101}, {"file_ids": [True]},
    {"file_ids": [""]}, {"file_ids": ["file-1"], "title": "a" * 181},
    {"file_ids": ["file-1"], "expires_days": True}, {"file_ids": ["file-1"], "expires_days": 3651},
    {"file_ids": ["file-1"], "access_code": "<script>"}, {"file_ids": ["file-1"], "auto_access_code": 1},
    {"file_ids": ["file-1"], "allow_download": 0}, {"file_ids": ["file-1"], "max_restore_count": -1},
])
def test_invalid_share_policy_never_reaches_provider(arguments):
    client = ShareClient()
    client.file_info = Mock(side_effect=AssertionError("invalid input reached Provider"))
    with pytest.raises(GuangYaShareError):
        client.prepare_user_share(**arguments)
    client.file_info.assert_not_called()
    assert client.writes == []
