"""光鸭回收站的真实对象预检、冻结复验和受理回执，不保存另一份计划。"""
from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from app.clients.guangya import GuangYaClient, GuangYaFile, GuangYaWriteRejected, guangya_provider_task_state

MAX_RECYCLE_ITEMS = 20_000
MAX_RESTORE_ITEMS = 200


class GuangYaRecycleError(RuntimeError):
    code = "precondition_failed"


class GuangYaRecycleStale(GuangYaRecycleError):
    code = "confirmation_stale"


def _bounded_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, min(int(value), (1 << 63)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _snapshot(item: GuangYaFile) -> dict[str, Any]:
    return {"file_id": item.file_id, "name": item.name, "is_dir": item.is_dir,
            "size": _bounded_int(item.size), "etag": item.etag or "",
            "parent_id": item.parent_id or "0", "updated_at": _bounded_int(item.updated_at)}


def _read(client: GuangYaClient) -> dict[str, Any]:
    if not client.logged_in:
        raise GuangYaRecycleError("光鸭账号尚未连接")
    generation = client.credential_generation
    items = client.list_recycle(max_items=MAX_RECYCLE_ITEMS)
    if client.credential_generation != generation:
        raise GuangYaRecycleStale("光鸭登录凭据已变化，请重新查询回收站")
    return {"credential_generation": generation,
            "items": [_snapshot(item) for item in sorted(items, key=lambda item: item.file_id)]}


def prepare_restore(client: GuangYaClient, file_ids: list[str]) -> dict[str, Any]:
    """按真实 ID 冻结选中对象；完整读取失败不接受部分列表。"""
    if (not isinstance(file_ids, list) or not 1 <= len(file_ids) <= MAX_RESTORE_ITEMS
            or any(not isinstance(value, str) or not value.strip() for value in file_ids)):
        raise GuangYaRecycleError(f"file_ids 必须包含 1 到 {MAX_RESTORE_ITEMS} 个非空对象 ID")
    selected = set(value.strip() for value in file_ids)
    snapshot = _read(client)
    items = [item for item in snapshot["items"] if item["file_id"] in selected]
    if len(items) != len(selected):
        raise GuangYaRecycleStale("选择的对象已不在光鸭回收站，请重新查询")
    return {**snapshot, "items": items}


def prepare_clear(client: GuangYaClient) -> dict[str, Any]:
    """冻结整个回收站的无序集合；空站不生成永久删除计划。"""
    snapshot = _read(client)
    if not snapshot["items"]:
        raise GuangYaRecycleError("光鸭回收站已经为空")
    return snapshot


def _submit(client: GuangYaClient, snapshot: dict[str, Any], *, clear: bool,
            cancel_check: Callable[[], None] | None) -> dict[str, Any]:
    if client.credential_generation != snapshot["credential_generation"]:
        raise GuangYaRecycleStale("光鸭登录凭据已变化，请重新查询回收站")
    current = _read(client)
    expected = snapshot["items"]
    if clear:
        matches = current == snapshot
    else:
        by_id = {item["file_id"]: item for item in current["items"]}
        matches = all(
            item["file_id"] in by_id
            and all(by_id[item["file_id"]][key] == item[key] for key in ("name", "is_dir", "size"))
            and (not item["etag"] or by_id[item["file_id"]]["etag"] in {"", item["etag"]})
            for item in expected
        )
    if not matches or current["credential_generation"] != snapshot["credential_generation"]:
        raise GuangYaRecycleStale("光鸭回收站内容已变化，请重新查询后再确认")
    if cancel_check is not None:
        cancel_check()
    if client.credential_generation != snapshot["credential_generation"]:
        raise GuangYaRecycleStale("光鸭登录凭据已变化，请重新查询回收站")
    receipt = {"count": len(expected), "total_size": sum(item["size"] for item in expected),
               "verified": False, "verification_pending": True,
               "operation": "recycle_clear" if clear else "recycle_restore"}
    if clear:
        receipt["irreversible"] = True
    try:
        task_id = client.clear_recycle_bin() if clear else client.restore_from_recycle(
            [item["file_id"] for item in expected])
    except GuangYaWriteRejected:
        raise
    except Exception:
        # 已进入 Provider 写边界；查询/重发不能证明原写入未发生。
        return {**receipt, "ok": False, "status": "outcome_unknown", "task_id": "",
                "error": "光鸭回收站操作结果未知，请先核对回收站，勿重复提交"}
    return {**receipt, "ok": True, "status": "accepted", "task_id": task_id}


def restore_prepared(client: GuangYaClient, snapshot: dict[str, Any], *,
                     cancel_check: Callable[[], None] | None = None) -> dict[str, Any]:
    """复验原选中快照并提交一次恢复；Provider 受理不代表任务完成。"""
    return _submit(client, snapshot, clear=False, cancel_check=cancel_check)


def clear_prepared(client: GuangYaClient, snapshot: dict[str, Any], *,
                   cancel_check: Callable[[], None] | None = None) -> dict[str, Any]:
    """仅在完整集合不变时提交全局清空；Provider 无原子集合删除前置条件。"""
    return _submit(client, snapshot, clear=True, cancel_check=cancel_check)


def read_task(client: GuangYaClient, task_id: str) -> dict[str, Any]:
    """为既有后台回执读取归一状态；不改变 cloud.task 的 Provider 原始响应。"""
    if not client.logged_in:
        raise GuangYaRecycleError("光鸭账号尚未连接")
    raw = client.task_status(task_id)
    status = guangya_provider_task_state(raw)
    payload = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    progress = payload.get("progress")
    if progress is None:
        progress = payload.get("percent")
    try:
        value = float(progress or 0)
        if isinstance(progress, bool) or not math.isfinite(value):
            value = 0.0
        if value > 1:
            value /= 100
    except (TypeError, ValueError, OverflowError):
        value = 0.0
    return {"status": status, "progress": 1.0 if status == "completed" else max(0.0, min(value, 1.0))}
