"""Web、Telegram 和 SDK 共用的逐项审阅；执行仍由原确认队列负责。"""
from __future__ import annotations

import hashlib
import json

from app import database as db
from app.modules import organize_confirmations as confirmations
from app.modules.organize import Organizer, OrganizeRules, organize_rules_snapshot_changes
from app.modules.scraper import MatchResult
from app.sensitive_data import redact_sensitive_text

MAX_SELECTION = 1000


def _tokens(values: object) -> list[str]:
    if (not isinstance(values, list) or not 1 <= len(values) <= MAX_SELECTION
            or any(not isinstance(value, str) or not value or len(value) > 100 for value in values)):
        raise ValueError(f"请选择 1–{MAX_SELECTION} 组待确认媒体")
    return list(dict.fromkeys(values))


def _row(token: str, chat_id: str | None = None):
    row = db.get_organize_confirmation(token)
    if row is None or (chat_id is not None and row["chat_id"] != chat_id):
        raise ValueError("确认操作不存在或已失效")
    return row


def _candidate_view(candidate: dict, index: int) -> dict:
    result = {key: candidate.get(key, "") for key in (
        "title", "year", "provider", "external_id", "tmdb_id", "media_type",
    )}
    metadata = candidate.get("metadata") or {}
    result["evidence"] = {key: metadata[key] for key in (
        "number", "release_date", "actors", "maker", "label", "series",
    ) if key in metadata}
    result["index"] = index
    return result


def _view(row) -> dict:
    payload = confirmations._decode_row(row)
    review = confirmations._decode_json_object(row["review_result_json"])
    candidates = [_candidate_view(candidate, index) for index, candidate in enumerate(payload.get("candidates") or [])]
    for evidence in review.get("evidence") or []:
        index = evidence.get("candidate_index")
        if type(index) is int and 0 <= index < len(candidates):
            candidates[index]["evidence"] = {key: evidence[key] for key in (
                "number", "release_date", "year", "actors", "actors_truncated", "maker", "label", "director", "series", "source_provider",
                "external_id", "source_number_matches", "frozen_identity_matches",
            ) if key in evidence}
    index = review.get("candidate_index")
    suggestion = None
    if row["review_status"] == "approved" and type(index) is int and 0 <= index < len(candidates):
        suggestion = {key: review.get(key, "") for key in (
            "candidate_index", "summary", "reason_code", "confidence",
        )}
        suggestion["source"] = review.get("source") or "agent"
        suggestion["summary"] = redact_sensitive_text(suggestion["summary"])
    return {
        "token": row["token"], "task_id": row["organize_task_id"],
        "batch_id": str(payload.get("_confirmation_batch") or ""),
        "source": row["source_name"], "directory": row["directory_path"],
        "identity": payload.get("identity") or row["directory_path"],
        "reason": payload.get("reason") or "", "status": (
            "expired" if row["status"] == "pending" and row["expires_at"] <= db.now() else row["status"]
        ),
        "expires_at": row["expires_at"], "candidates": candidates,
        "files": [{key: file.get(key) for key in ("file_id", "name", "size", "season", "episode")}
                  for file in payload.get("files") or []],
        "review_status": row["review_status"], "suggestion": suggestion,
        "review_summary": redact_sensitive_text(review.get("summary") or review.get("reason_code") or ""),
        "selected_index": row["selected_index"], "error": redact_sensitive_text(row["error"] or ""),
        "downstream": confirmations._decode_json_object(row["result_json"]).get("_downstream") or {},
    }


def list_confirmations(
    *, batch_id: str = "", task_id: str = "", source: str = "", reason: str = "",
    q: str = "", status: str = "pending", tokens: list[str] | None = None,
    page: int = 1, page_size: int = 20, chat_id: str | None = None,
) -> dict:
    if not isinstance(status, str) or status not in {"all", "pending", "queued", "running", "completed", "failed", "expired", "cancelled"}:
        raise ValueError("确认状态无效")
    if any(not isinstance(value, str) or len(value) > 500 for value in (batch_id, task_id, source, reason, q)):
        raise ValueError("筛选参数无效")
    if type(page) is not int or page < 1 or type(page_size) is not int or not 1 <= page_size <= MAX_SELECTION:
        raise ValueError("分页参数无效")
    result = db.query_organize_confirmations(
        batch_id=batch_id, task_id=task_id, source=source, reason=reason,
        keyword=q, status=status, tokens=_tokens(tokens) if tokens is not None else None,
        chat_id=chat_id, page=page, page_size=page_size, selection_limit=MAX_SELECTION,
    )
    return {
        "items": [_view(row) for row in result["rows"]],
        "tokens": result["tokens"], "total": result["total"],
        "page": page, "pages": (result["total"] + page_size - 1) // page_size,
        "counts": result["counts"],
        "selection_truncated": result["total"] > len(result["tokens"]),
        "review_enabled": confirmations._recognition_review_is_enabled(),
    }


def request_reviews(tokens: list[str], *, chat_id: str | None = None) -> dict:
    values = _tokens(tokens)
    if not confirmations._recognition_review_is_enabled():
        raise ValueError("请先在 Agent 设置中启用识别复核并配置模型；也可直接逐项选择候选")
    result = db.request_organize_confirmation_reviews(values, chat_id=chat_id)
    confirmations.wake_recognition_review_dispatcher()
    return result


def save_selection(token: str, candidate_index: int, *, chat_id: str | None = None) -> dict:
    _tokens([token])
    db.save_organize_confirmation_selection(token, candidate_index, chat_id=chat_id)
    return {"success": True}


def _choices(entries: list[dict], chat_id: str | None) -> list[tuple]:
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("批量候选参数无效")
    tokens = _tokens([entry.get("token") for entry in entries])
    if len(tokens) != len(entries):
        raise ValueError("同一媒体组只能选择一个候选")
    result = []
    for entry in entries:
        row = _row(entry["token"], chat_id)
        payload = confirmations._decode_row(row)
        if confirmations._confirmation_kind(payload) != "guangya":
            raise ValueError("当前批量审阅仅支持光鸭整理，本地任务请使用原确认入口")
        index = entry.get("candidate_index")
        candidates = payload.get("candidates") or []
        if type(index) is not int or not 0 <= index < len(candidates):
            raise ValueError("候选参数无效")
        retry_index = payload.get(confirmations._RETRY_SELECTED_INDEX_KEY)
        if retry_index is not None and retry_index != index:
            raise ValueError("该重试确认已绑定其他候选")
        if row["status"] == "pending" and row["expires_at"] <= db.now():
            raise ValueError("候选已过期，请重新整理生成候选")
        if row["status"] not in {"pending", "queued", "running", "completed"}:
            raise ValueError("候选已失效或执行失败，请刷新并核对原记录")
        if row["status"] != "pending" and row["selected_index"] != index:
            raise ValueError("该媒体已选择其他候选，请刷新")
        result.append((row, payload, index))
    return result


def _digest(choices: list[tuple]) -> str:
    # 通知位置、模型建议和运行状态不属于文件变更范围；其更新不使已确认操作失去幂等性。
    frozen = [{"token": row["token"], "index": index,
               "episode_research_receipt": confirmations._episode_research_receipt_from_row(row),
               "payload": {key: payload.get(key) for key in (
                   "kind", "source_dir_id", "source_parent_id", "files", "companions", "candidates", "rules",
                   "rules_snapshot", "snapshot_digest", "local_task_id", "local_task_version", "download_request_ids",
               )}} for row, payload, index in choices]
    return hashlib.sha256(json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _episode_mappings(row, index: int) -> list[dict]:
    receipt = confirmations._episode_research_receipt_from_row(row)
    if receipt is None:
        return []
    from app.modules.episode_research_service import POLICY_VERSION, proposal_receipt
    from app.repositories.episode_research import get_episode_research_cache
    stored = get_episode_research_cache(str(receipt.get("cache_key") or ""), policy_version=POLICY_VERSION)
    proposal = (stored or {}).get("payload", {}).get("proposal")
    if (receipt.get("candidate_index") != index or not stored or stored["status"] != "verified"
            or not isinstance(proposal, dict) or proposal_receipt(proposal) != receipt):
        raise ValueError("季集研究证据已变化，请重新复核或保存人工选择")
    return list(proposal["mappings"])


def preview_choices(entries: list[dict], *, chat_id: str | None = None) -> dict:
    choices = _choices(entries, chat_id)
    items = []
    # 仅使用命名器的纯函数，不为100个待确认项重新扫描云盘或拉取整部剧详情。
    organizer = Organizer(local_only=True, recognition_enabled=False)
    try:
        for row, payload, index in choices:
            candidate = payload["candidates"][index]
            current = OrganizeRules.from_config().for_source(str(payload.get("source_dir_id") or ""))
            if confirmations._confirmation_kind(payload) == "guangya" and row["status"] == "pending":
                changes = organize_rules_snapshot_changes(payload.get("rules"), current, files=payload.get("files"))
                if changes:
                    raise ValueError("整理规则已变化，请重新整理生成候选")
            match = MatchResult(
                title=str(candidate.get("title") or ""), year=str(candidate.get("year") or ""),
                provider=str(candidate.get("provider") or "tmdb"), external_id=str(candidate.get("external_id") or ""),
                tmdb_id=str(candidate.get("tmdb_id") or ""), media_type=str(candidate.get("media_type") or ""),
            )
            mappings = _episode_mappings(row, index)
            positions = {mapping["file_index"]: mapping for mapping in mappings}
            files = [{**file, **({"season": positions[i]["target_season"], "episode": positions[i]["target_episode"]}
                                if i in positions else {})} for i, file in enumerate(payload.get("files") or [])]
            targets = list(dict.fromkeys("/".join(organizer.build_media_path_parts(match, file, current)) for file in files))
            view = _view(row)
            items.append({**view, "candidate": _candidate_view(candidate, index),
                          "target": "；".join(targets), "target_root_id": current.target_dir_id,
                          "episode_mappings": mappings})
    finally:
        organizer.close()
    return {"entries": [{"token": row["token"], "candidate_index": index} for row, _payload, index in choices],
            "digest": _digest(choices), "items": items,
            "note": "预览媒体目录；分类层级及文件名由现有整理规则生成，执行前仍会核验源文件、元数据及目标冲突。"}


def submit_choices(entries: list[dict], digest: str, *, chat_id: str | None = None, actor: str = "human") -> dict:
    choices = _choices(entries, chat_id)
    if not isinstance(digest, str) or not digest or digest != _digest(choices):
        raise ValueError("预览已变化，请重新预览后确认")
    items = []
    for row, _payload, index in choices:
        try:
            execution_actor = "agent" if confirmations._episode_research_receipt_from_row(row) else actor
            result = confirmations.start_confirmation(
                row["token"], index, chat_id=str(row["chat_id"] or ""), actor=execution_actor,
                explicit_approval=True, dispatch=False,
                expected_snapshot=(row["payload_json"], row["review_result_json"]),
            )
            items.append({**result, "token": row["token"], "accepted": True})
        except ValueError as exc:
            items.append({"token": row["token"], "accepted": False, "status": "rejected",
                          "error": redact_sensitive_text(str(exc))})
    confirmations.wake_confirmation_dispatcher()
    return {"items": items, "accepted": sum(bool(item["accepted"]) for item in items),
            "failed": sum(not item["accepted"] for item in items)}
