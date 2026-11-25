"""使用现有光鸭会话按 GCID 秒传并核验目标文件。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from threading import Lock
from time import sleep

from app.clients.guangya import (
    GuangYaClient,
    GuangYaFile,
    GuangYaReadRejected,
    GuangYaWriteRejected,
    _read_success_acknowledged,
    _validate_read_response,
    _validate_write_response,
    file_info_or_none,
)

from .gcid_manifest import ManifestValidationError, _normalize_gcid, _normalize_path, _normalize_size

_API = "https://api.guangyapan.com/userres/v1"
_TOKEN = "/get_res_center_token"
_FLASH = "/check_can_flash_upload"
_STATUS = "/file/get_info_by_task_id"
_CANCEL = "/file/delete_upload_task"
_STATUS_ATTEMPTS = 4


@dataclass(frozen=True, slots=True)
class GCIDImportItemResult:
    success: bool
    remote_file_id: str = ""
    error_code: str = ""


class _ImportFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _failure_code(exc: Exception) -> str:
    if isinstance(exc, _ImportFailure):
        return exc.code
    if GuangYaClient._rate_limit_kind(exc) or (
        isinstance(exc, GuangYaWriteRejected)
        and GuangYaReadRejected(exc.operation, code=exc.code, message=exc.public_message).rate_limited
    ):
        return "rate_limited"
    if isinstance(exc, (GuangYaReadRejected, GuangYaWriteRejected)):
        return "provider_rejected"
    return "request_failed"


def _payload(response: dict) -> dict:
    data = response.get("data")
    return data if isinstance(data, dict) else response


def _reported_id(data: dict, key: str) -> str:
    value = data.get(key)
    if isinstance(value, str):
        return value.strip()
    if type(value) is int and value > 0:
        return str(value)
    return ""


def _reported_file_id(response: dict) -> str:
    for payload in (_payload(response), response):
        file_id = _reported_id(payload, "fileId")
        file_info = payload.get("fileInfo")
        if not file_id and isinstance(file_info, dict):
            file_id = _reported_id(file_info, "fileId")
        if file_id:
            return file_id
    return ""


def _can_confirm_miss(response: dict) -> bool:
    return str(response.get("code")) != "156" and all(
        not (payload.get("fileId") or payload.get("fileInfo"))
        and ("canFlashUpload" not in payload or payload["canFlashUpload"] is False)
        and not any(payload.get(key) is not None for key in ("status", "taskStatus", "state"))
        and str(payload.get("msg") or payload.get("message") or "").strip().lower() in {"", "success", "ok", "成功"}
        for payload in (response, _payload(response))
    )


def _pending(response: dict) -> bool:
    return str(response.get("code")) == "147" and response.get("msg") == "文件上传中"


def _pending_for(response: dict, task_id: str) -> bool:
    return _pending(response) and not any(
        payload.get("fileId") or payload.get("fileInfo")
        or (payload.get("taskId") is not None and _reported_id(payload, "taskId") != task_id)
        or any(payload.get(key) is not None for key in ("status", "taskStatus", "state"))
        for payload in (response, _payload(response))
    )


class GuangyaGCIDImporter:
    def __init__(self, client: GuangYaClient):
        self.client = client
        self._directory_ids: dict[tuple[str, str], str] = {}
        self._lock = Lock()

    def import_file(
        self, *, target_dir_id: str, path: str, size: int, gcid: str, md5: str = "",
    ) -> GCIDImportItemResult:
        try:
            *directories, name = _normalize_path(path, 0).split("/")
            size = _normalize_size(size, 0)
            gcid = _normalize_gcid(gcid, 0).lower()
            target_dir_id = target_dir_id.strip()
            md5 = md5.strip().lower()
            if md5 and not re.fullmatch(r"[0-9a-f]{32}", md5):
                raise ManifestValidationError("MD5 格式无效")
        except (ManifestValidationError, AttributeError):
            return GCIDImportItemResult(False, error_code="invalid_input")
        if not target_dir_id:
            return GCIDImportItemResult(False, error_code="invalid_target")

        try:
            with self._lock:
                if target_dir_id != "0":
                    target = file_info_or_none(self.client.file_info, target_dir_id)
                    if target is None or target.file_id != target_dir_id or not target.is_dir:
                        raise _ImportFailure("invalid_target")
                parent_id = target_dir_id
                for directory in directories:
                    parent_id = self._directory(parent_id, directory)
                existing = self._child(parent_id, name)
                if existing is not None:
                    file_id = self._existing_file(existing, parent_id, size, gcid)
                else:
                    file_id = self._flash_file(parent_id, name, size, gcid, md5)
                return GCIDImportItemResult(True, remote_file_id=file_id)
        except Exception as exc:
            return GCIDImportItemResult(False, error_code=_failure_code(exc))

    def _child(self, parent_id: str, name: str) -> GuangYaFile | None:
        matches = [item for item in self.client.list_dir(parent_id) if item.name.casefold() == name.casefold()]
        if not matches:
            return None
        if len(matches) != 1 or matches[0].name != name:
            raise _ImportFailure("name_conflict")
        child = matches[0]
        if not child.file_id or child.parent_id != parent_id:
            raise _ImportFailure("verify_failed")
        return child

    def _directory(self, parent_id: str, name: str) -> str:
        key = (parent_id, name)
        cached_id = self._directory_ids.get(key)
        if cached_id:
            current = file_info_or_none(self.client.file_info, cached_id)
            if current and current.file_id == cached_id and current.is_dir and (current.parent_id, current.name) == key:
                return cached_id
            self._directory_ids.pop(key)

        child = self._child(parent_id, name)
        if child is None:
            try:
                created_id = self.client.create_dir(name, parent_id)
            except Exception as exc:
                if _failure_code(exc) == "rate_limited":
                    raise
                # 并发创建或响应丢失时重新读取一次，绝不重复提交建目录。
                child = self._child(parent_id, name)
                if child is None:
                    raise
            else:
                child = self.client.file_info(created_id) if created_id else None
                if child is None:
                    child = self._child(parent_id, name)
                if child is None or (created_id and child.file_id != created_id):
                    raise _ImportFailure("verify_failed")
        if not child.is_dir:
            raise _ImportFailure("name_conflict")
        if not child.file_id or (child.parent_id, child.name) != key:
            raise _ImportFailure("verify_failed")
        self._directory_ids[key] = child.file_id
        return child.file_id

    def _existing_file(self, item: GuangYaFile, parent_id: str, size: int, gcid: str) -> str:
        if item.is_dir or item.size != size:
            raise _ImportFailure("name_conflict")
        if item.etag.strip():
            if item.etag.strip().lower() != gcid:
                raise _ImportFailure("name_conflict")
            return item.file_id
        return self._verify_file(parent_id, item.name, size, gcid, item.file_id)

    def _verify_file(self, parent_id: str, name: str, size: int, gcid: str, file_id: str) -> str:
        item = self.client.file_info(file_id)
        if (
            item is None or item.file_id != file_id
            or item.is_dir or (item.parent_id, item.name, item.size) != (parent_id, name, size)
            or item.etag.strip().lower() != gcid
        ):
            raise _ImportFailure("verify_failed")
        return item.file_id

    def _request(self, path: str, body: dict) -> dict:
        reply = self.client.raw.request(_API + path, method="POST", json=body)
        reply.raise_for_status()
        response = reply.json()
        if not isinstance(response, dict):
            raise _ImportFailure("request_failed")
        instant = path in {_TOKEN, _FLASH} and str(response.get("code")) == "156"
        checked = {**response, "code": 0} if instant or (path == _STATUS and _pending(response)) else response
        _validate_read_response(checked, operation=path)
        if not instant:
            _validate_write_response(checked, operation=path)
        if not (_read_success_acknowledged(checked) or _read_success_acknowledged(_payload(checked))):
            raise _ImportFailure("request_failed")
        return response

    def _flash_file(self, parent_id: str, name: str, size: int, gcid: str, md5: str) -> str:
        res: dict[str, int | str] = {"fileSize": size, "gcid": gcid}
        if md5:
            res["md5"] = md5
        token = self._request(_TOKEN, {
            "capacity": 2, "name": name, "res": res, "parentId": "" if parent_id == "0" else parent_id,
        })
        task_ids = {_reported_id(payload, "taskId") for payload in (token, _payload(token)) if "taskId" in payload}
        if len(task_ids) > 1:
            raise _ImportFailure("verify_failed")
        task_id = next(iter(task_ids), "")
        file_id = _reported_file_id(token)
        miss_candidate = False
        if str(token.get("code")) != "156" and not file_id:
            if not task_id:
                raise _ImportFailure("request_failed")
            result = self._request(_FLASH, {"taskId": task_id, **res})
            data = _payload(result)
            reported_task_id = _reported_id(data, "taskId") or _reported_id(result, "taskId")
            file_id = _reported_file_id(result)
            miss_candidate = _can_confirm_miss(result)
            if miss_candidate and any(
                "taskId" in payload and _reported_id(payload, "taskId") != task_id
                for payload in (result, data)
            ):
                raise _ImportFailure("verify_failed")
            task_id = reported_task_id or task_id
        if file_id:
            return self._verify_file(parent_id, name, size, gcid, file_id)

        status = self._await_status(task_id, attempts=_STATUS_ATTEMPTS - int(miss_candidate)) if task_id else {}
        file_id = _reported_file_id(status)
        if file_id:
            return self._verify_file(parent_id, name, size, gcid, file_id)
        landed = self._child(parent_id, name)
        if landed is not None:
            return self._existing_file(landed, parent_id, size, gcid)
        if miss_candidate and _pending_for(status, task_id):
            # 目标仍为空后再查一次，确保只取消本次仍未完成的上传任务。
            status = self._request(_STATUS, {"taskId": task_id})
            file_id = _reported_file_id(status)
            if file_id:
                return self._verify_file(parent_id, name, size, gcid, file_id)
            if _pending_for(status, task_id):
                self._cancel_task(task_id)
                raise _ImportFailure("not_matched")
        raise _ImportFailure("verify_failed")

    def _await_status(self, task_id: str, *, attempts: int) -> dict:
        response: dict = {}
        for attempt in range(attempts):
            response = self._request(_STATUS, {"taskId": task_id})
            if not _pending_for(response, task_id):
                break
            if attempt + 1 < attempts:
                sleep(1)
        return response

    def _cancel_task(self, task_id: str) -> None:
        try:
            reply = self._request(_CANCEL, {"taskIds": [task_id]})
        except Exception as exc:
            raise _ImportFailure("cancel_failed") from exc
        task_ids = _payload(reply).get("taskIds")
        if task_ids is not None and (not isinstance(task_ids, list) or task_id not in task_ids):
            raise _ImportFailure("cancel_failed")
