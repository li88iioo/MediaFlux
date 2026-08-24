"""实用工具 API：刮削预览、命名预览、映射锁管理、代理连通性测试。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from time import perf_counter
from urllib.parse import urlparse

import httpx
import requests
from fastapi import APIRouter, Body, Path, Query, Request
from fastapi.responses import Response

from app import config
from app import database as db
from app.agent.async_bridge import run_awaitable_sync
from app.agent.rate_limit import agent_rate_limiter
from app.clients.openai_compatible import (
    PROTOCOLS,
    SUPPORTED_PROTOCOLS_TEXT,
    extract_output_text,
    is_protocol_fallback_error,
    iter_provider_text_deltas,
    native_tool_definitions,
    native_tool_initial_history,
    native_tool_request_body,
    normalize_provider_location,
    parse_native_tool_turn,
    protocol_attempts,
    provider_headers,
    resolve_protocol,
    structured_request_body,
    text_stream_request_body,
)
from app.indexers.errors import IndexerError
from app.indexers.http import FixedHostHttpClient
from app.logger import get_logger, redact_sensitive_text
from app.modules import gcid_import
from app.modules.gcid_manifest import ManifestValidationError, validate_manifest
from app.modules.scraper import (
    Candidate, MatchResult, TMDBScraper, decide_threshold,
    extract_recognition_context, generate_query_variants,
)
from app.modules import recognition_preprocess_rules, tmdb_regex_rules
from app.web import api_error, api_response, csrf_token, require_api_login

logger = get_logger(__name__)
router = APIRouter(prefix="/api/tools")


def _safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name or "")


def _safe_ascii_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name or "").strip("._")


def _tmdb_image_url(path: object, size: str = "w500") -> str:
    value = str(path or "").strip()
    if not value:
        return ""
    if value.startswith("https://"):
        return value
    if value.startswith("http://"):
        return ""
    return f"https://image.tmdb.org/t/p/{size}/{value.lstrip('/')}"


def _names(items: object, *, key: str = "name") -> list[str]:
    if not isinstance(items, list):
        return []
    values = []
    for item in items:
        value = item.get(key) if isinstance(item, dict) else item
        text = str(value or "").strip()
        if text and text not in values:
            values.append(text)
    return values


def _serialize_score_breakdown(breakdown) -> dict | None:
    if breakdown is None:
        return None
    return {
        "title_score": float(breakdown.title_score),
        "original_title_score": float(breakdown.original_title_score),
        "alias_score": float(breakdown.alias_score),
        "year_score": float(breakdown.year_score),
        "year_penalty": float(breakdown.year_penalty),
        "media_type_score": float(breakdown.media_type_score),
        "constraint_penalty": float(breakdown.constraint_penalty),
        "final_score": float(breakdown.final_score),
        "matched_title": str(breakdown.matched_title or ""),
        "matched_query": str(breakdown.matched_query or ""),
        "rejected_constraints": list(breakdown.rejected_constraints or []),
    }


def _serialize_candidate(candidate: Candidate) -> dict:
    return {
        "tmdb_id": candidate.tmdb_id,
        "title": candidate.title,
        "original_title": candidate.original_title,
        "aliases": list(candidate.aliases or []),
        "year": candidate.year,
        "release_date": candidate.release_date,
        "score": candidate.score,
        "score_breakdown": _serialize_score_breakdown(candidate.score_breakdown),
        "media_type": candidate.media_type,
        "overview": candidate.overview,
        "poster_url": _tmdb_image_url(candidate.poster_path, "w342"),
        "backdrop_url": _tmdb_image_url(candidate.backdrop_path, "w780"),
    }


def _serialize_recognition(result: MatchResult, filename: str, parent_path: str) -> dict:
    context = getattr(result, "context", None) or extract_recognition_context(filename, parent_path)
    queries = list(getattr(result, "query_variants", None) or generate_query_variants(context))
    rejected = list(getattr(result, "rejected_constraints", None) or [])
    decision = dict(getattr(result, "threshold_decision", None) or decide_threshold(
        result.confidence, result.threshold, rejected,
    ))
    ai_raw = getattr(result, "ai_diagnostic", None) or {}
    ai_input = ai_raw.get("input") if isinstance(ai_raw.get("input"), dict) else {}
    ai_output = ai_raw.get("output") if isinstance(ai_raw.get("output"), dict) else {}
    ai_second = (
        ai_raw.get("second_search")
        if isinstance(ai_raw.get("second_search"), dict)
        else {}
    )
    ai_position_guard = (
        ai_raw.get("position_guard")
        if isinstance(ai_raw.get("position_guard"), dict)
        else {}
    )
    ai_revalidation = (
        ai_raw.get("tmdb_revalidation")
        if isinstance(ai_raw.get("tmdb_revalidation"), dict)
        else {}
    )
    return {
        "normalized_title": context.normalized_title,
        "filename_title": context.filename_title,
        "filename_year": context.filename_year,
        "cleaned_components": context.cleaned_components,
        "title_variants": list(context.title_variants),
        "folder_context": {
            "path": parent_path,
            "title": context.folder_title,
            "year": context.folder_year,
            "media_type": context.media_type,
            "season": context.season,
            "episode": context.episode,
        },
        "query_variants": queries,
        "preprocess": {
            "evaluated": bool(getattr(result, "preprocess_evaluated", False)),
            "filename": str(getattr(result, "recognition_filename", "") or filename),
            "parent_path": str(getattr(result, "recognition_parent_path", "") or parent_path),
            "effective_season": getattr(result, "effective_season", None),
            "effective_episode": getattr(result, "effective_episode", None),
            "applied_rules": list(getattr(result, "preprocess_rules", None) or []),
        },
        "rejected_constraints": rejected,
        "threshold_decision": decision,
        "ai": {
            "attempted": ai_raw.get("attempted") is True,
            "reason": str(ai_raw.get("reason") or ""),
            "input": {
                key: ai_input.get(key)
                for key in (
                    "normalized_title", "filename_title", "folder_title",
                    "folder_year", "media_type", "season", "episode", "aliases",
                )
                if key in ai_input
            },
            "output": {
                key: ai_output.get(key)
                for key in (
                    "title", "original_title", "year", "media_type",
                    "season", "episode", "aliases", "confidence",
                )
                if key in ai_output
            },
            "confidence_threshold": ai_raw.get("confidence_threshold"),
            "second_search": {
                "candidate_count": ai_second.get("candidate_count"),
                "threshold_decision": ai_second.get("threshold_decision"),
            } if ai_second else {},
            "position_guard": dict(ai_position_guard),
            "tmdb_revalidation": dict(ai_revalidation),
            "error": redact_sensitive_text(ai_raw.get("error") or "")[:500],
        },
    }


def _serialize_match(result: MatchResult, detail: dict) -> dict:
    media_type = "tv" if result.media_type == "tv" else "movie"
    release_date = str(
        detail.get("first_air_date") or detail.get("release_date") or ""
    )
    origin = detail.get("origin_country")
    if not origin:
        origin = _names(detail.get("production_countries"))
    elif isinstance(origin, list):
        origin = [str(item) for item in origin if str(item).strip()]
    else:
        origin = [str(origin)]
    return {
        "tmdb_id": result.tmdb_id,
        "title": str(detail.get("name") or detail.get("title") or result.title or ""),
        "original_title": str(
            detail.get("original_name") or detail.get("original_title") or ""
        ),
        "year": release_date[:4] or result.year,
        "release_date": release_date,
        "media_type": media_type,
        "confidence": result.confidence,
        "overview": str(detail.get("overview") or ""),
        "vote_average": detail.get("vote_average"),
        "vote_count": detail.get("vote_count"),
        "status": str(detail.get("status") or ""),
        "tagline": str(detail.get("tagline") or ""),
        "genres": _names(detail.get("genres")),
        "origin_country": origin,
        "spoken_languages": _names(detail.get("spoken_languages")),
        "networks": _names(detail.get("networks")),
        "production_companies": _names(detail.get("production_companies")),
        "season_count": detail.get("number_of_seasons") if media_type == "tv" else None,
        "episode_count": detail.get("number_of_episodes") if media_type == "tv" else None,
        "poster_url": _tmdb_image_url(detail.get("poster_path"), "w500"),
        "backdrop_url": _tmdb_image_url(detail.get("backdrop_path"), "w1280"),
        "homepage": str(detail.get("homepage") or ""),
    }


@router.post("/scrape/preview")
def scrape_preview(request: Request, data: dict | None = Body(default=None)):
    """输入文件名，返回识别诊断、TMDB 详情、候选和最终命名预览。"""
    require_api_login(request)
    data = data or {}
    filename = (data.get("filename") or "").strip()
    parent_path = str(data.get("parent_path") or "").strip()[:4096]
    if not filename:
        return api_error("请输入文件名", 400)
    try:
        scraper = TMDBScraper()
        result = scraper.match(filename, parent_path) if parent_path else scraper.match(filename)
        release_parse = scraper.parse_media(filename, parent_path, result)
        parsed: dict[str, object] = {
            "title": release_parse.title,
            "year": release_parse.year,
            "type": release_parse.media_type,
            "season": release_parse.effective_season,
            "episode": release_parse.effective_episode,
        }
        if release_parse.tmdb_id:
            parsed["tmdb_id"] = release_parse.tmdb_id
        if result.matched_by == "ai_search":
            result.need_confirm = True
            result.locked = False
            result.status = "low_confidence"
            result.error = result.error or "AI 候选必须人工确认并锁定后才能用于整理"
        detail = scraper.get_detail(result.tmdb_id, result.media_type) if result.tmdb_id else {}
        detail_missing = bool(result.tmdb_id and not detail)
        parsed_payload = {
            **parsed,
            "resource_tags": scraper.parse_resource_tags(filename),
        }
        message = result.error or "匹配成功"
        if detail_missing:
            message = f"{message}；TMDB 详情暂不可用" if message else "TMDB 详情暂不可用"
        return api_response({
            "filename": filename,
            "parent_path": parent_path,
            "parsed": parsed_payload,
            "recognition": _serialize_recognition(result, filename, parent_path),
            "locked": bool(result.locked or parsed.get("tmdb_id")),
            "match": _serialize_match(result, detail),
            "candidates": [_serialize_candidate(candidate) for candidate in result.candidates],
            "need_confirm": result.need_confirm,
            "error": result.error,
            "diagnostic": {
                "status": result.status or ("matched" if result.tmdb_id else "no_result"),
                "message": message,
                "match_mode": scraper.match_mode,
                "threshold": result.threshold,
                "matched_by": result.matched_by,
                "regex_rule_id": result.regex_rule_id,
            },
            "naming": _build_naming_preview(
                result.tmdb_id,
                result.media_type,
                result.season_override if result.season_override is not None else parsed.get("season"),
                parsed.get("episode"),
                filename,
                result.title,
                result.year,
            ),
        })
    except Exception as exc:
        logger.error("刮削预览失败 type=%s", type(exc).__name__)
        return api_error(str(exc), 500)


def _build_naming_preview(
    tmdb_id: str,
    media_type: str,
    season,
    episode,
    filename: str,
    title: str = "",
    year: str = "",
) -> dict:
    """根据命名规则预览最终文件名与归档目录，复用实际整理命名层。"""
    from app.clients.guangya import GuangYaFile
    from app.modules.organize import OrganizeRules, Organizer
    from app.modules.scraper import MatchResult

    match = MatchResult(
        tmdb_id=str(tmdb_id or ""),
        title=str(title or ""),
        year=str(year or ""),
        media_type=str(media_type or ""),
    )
    rules = OrganizeRules.from_config()
    organizer = Organizer(client=object(), scraper=object())
    parsed = {"season": season, "episode": episode}
    file = GuangYaFile("preview", filename, False)
    file_name = organizer.build_new_name(match, file, parsed, rules) if tmdb_id else ""
    media_dir = organizer.build_media_dir(match, rules) if tmdb_id else ""
    return {
        "file_name": file_name,
        "show_dir": media_dir,
        "media_dir": media_dir,
        "full_target": f"{media_dir}/{file_name}" if media_dir and file_name else "",
    }


@router.post("/scrape/confirm")
def scrape_confirm(request: Request, data: dict | None = Body(default=None)):
    """人工锁定映射：raw_name → tmdb_id。"""
    require_api_login(request)
    data = data or {}
    raw_name = (data.get("filename") or "").strip()
    tmdb_id = (data.get("tmdb_id") or "").strip()
    title = (data.get("title") or "").strip()
    year = (data.get("year") or "").strip()
    media_type = (data.get("media_type") or "").strip()
    parent_path = str(data.get("parent_path") or "").strip()[:4096]
    rejected_tmdb_ids = [
        str(value or "").strip()
        for value in (data.get("rejected_tmdb_ids") or [])
        if str(value or "").strip()
    ] if isinstance(data.get("rejected_tmdb_ids") or [], list) else []
    if not raw_name or not tmdb_id:
        return api_error("缺少文件名或 tmdb_id", 400)
    try:
        confirm_kwargs = {"parent_path": parent_path}
        if rejected_tmdb_ids:
            confirm_kwargs["rejected_tmdb_ids"] = rejected_tmdb_ids
        TMDBScraper().confirm(
            raw_name, tmdb_id, title, year, media_type, **confirm_kwargs,
        )
        return api_response({"success": True})
    except Exception as exc:
        logger.error("映射锁定失败 type=%s", type(exc).__name__)
        return api_error(str(exc), 500)


@router.get("/locks")
def list_locks(request: Request, q: str = Query(default="")):
    require_api_login(request)
    rows = db.list_tmdb_locks(keyword=q or "", limit=300)
    return api_response([{
        "id": row["id"],
        "raw_name": row["raw_name"],
        "tmdb_id": row["tmdb_id"],
        "title": row["title"] or "",
        "year": row["year"] or "",
        "media_type": row["media_type"] or "",
        "parent_path": row["parent_path"] or "",
        "season": row["season"],
        "key_version": row["key_version"],
        "lock_source": str(row["lock_source"]),
        "locked_at": row["locked_at"],
    } for row in rows])


@router.delete("/locks/{lock_id}")
def delete_lock(request: Request, lock_id: int = Path(...)):
    require_api_login(request)
    ok = db.delete_tmdb_lock(lock_id)
    return api_response({"success": ok})


class TmdbRegexTargetUnavailable(RuntimeError):
    """TMDB 目标当前无法确认，不应归类为客户端格式错误。"""


def _validate_tmdb_regex_target(
    data: dict, *, filename: str = "", parent_path: str = "", media_type_hint: str = "",
) -> dict:
    """确认强制规则指向真实 TMDB 条目；any 必须由样例解析为单一类型。"""
    tmdb_id = str(data.get("tmdb_id") or "").strip()
    configured_type = str(data.get("media_type") or "any").strip().lower()
    if configured_type in {"movie", "tv"}:
        resolved_type = configured_type
    else:
        if not filename:
            raise ValueError("媒体类型选择“按样例识别”时必须填写样例文件名")
        resolved_type = str(media_type_hint or "").strip().lower()
        if resolved_type not in {"movie", "tv"}:
            resolved_type = extract_recognition_context(filename, parent_path).media_type
    match = TMDBScraper().match_from_tmdb(tmdb_id, resolved_type)
    if match.status != "matched" or not match.tmdb_id:
        type_label = "剧集" if resolved_type == "tv" else "电影"
        logger.warning(
            "TMDB 强制匹配目标校验失败 operation=validate tmdb=%s type=%s status=%s",
            tmdb_id, resolved_type, match.status,
        )
        raise TmdbRegexTargetUnavailable(
            f"暂时无法确认 TMDB {tmdb_id}（{type_label}）；请检查 ID、TMDB API 配置或稍后重试"
        )
    return {
        "tmdb_id": match.tmdb_id, "title": match.title, "year": match.year,
        "media_type": match.media_type,
    }


@router.get("/recognition-preprocess-rules")
def list_recognition_preprocess_rules_api(request: Request):
    require_api_login(request)
    rules = recognition_preprocess_rules.list_rules()
    return api_response({
        "rules": rules,
        "summary": {
            "total": len(rules),
            "enabled": sum(not item["disabled"] for item in rules),
            "builtin": sum(item["builtin"] for item in rules),
            "custom": sum(not item["builtin"] for item in rules),
        },
    })


@router.post("/recognition-preprocess-rules")
def create_recognition_preprocess_rule_api(
    request: Request, data: dict | None = Body(default=None),
):
    require_api_login(request)
    try:
        return api_response(recognition_preprocess_rules.create_rule(data or {}), 201)
    except ValueError as exc:
        return api_error(str(exc), 400)


@router.put("/recognition-preprocess-rules/{rule_id}")
def update_recognition_preprocess_rule_api(
    request: Request, rule_id: int = Path(...), data: dict | None = Body(default=None),
):
    require_api_login(request)
    try:
        return api_response(recognition_preprocess_rules.update_rule(rule_id, data or {}))
    except ValueError as exc:
        return api_error(str(exc), 404 if "不存在" in str(exc) else 400)


@router.delete("/recognition-preprocess-rules/{rule_id}")
def delete_recognition_preprocess_rule_api(
    request: Request, rule_id: int = Path(...),
):
    require_api_login(request)
    try:
        deleted = recognition_preprocess_rules.delete_rule(rule_id)
        if not deleted:
            return api_error("识别预处理规则不存在", 404)
        return api_response({"success": True})
    except ValueError as exc:
        return api_error(str(exc), 400)


@router.post("/recognition-preprocess-rules/preview")
def preview_recognition_preprocess_rules_api(
    request: Request, data: dict | None = Body(default=None),
):
    require_api_login(request)
    try:
        return api_response(recognition_preprocess_rules.preview_rules(data or {}))
    except ValueError as exc:
        return api_error(str(exc), 400)


@router.post("/recognition-preprocess-rules/restore-defaults")
def restore_recognition_preprocess_rules_api(request: Request):
    require_api_login(request)
    rules = recognition_preprocess_rules.restore_builtin_rules()
    return api_response({
        "rules": rules,
        "restored": sum(item["builtin"] for item in rules),
    })


@router.get("/tmdb-regex-rules")
def list_tmdb_regex_rules_api(request: Request):
    require_api_login(request)
    return api_response(tmdb_regex_rules.list_rules())


@router.post("/tmdb-regex-rules")
def create_tmdb_regex_rule_api(request: Request, data: dict | None = Body(default=None)):
    require_api_login(request)
    try:
        payload = dict(data or {})
        tmdb_regex_rules.preview_rule(payload, "", "", "movie")
        sample_filename = str(payload.pop("sample_filename", "") or "").strip()
        sample_parent_path = str(payload.pop("sample_parent_path", "") or "").strip()
        verified = _validate_tmdb_regex_target(
            payload, filename=sample_filename, parent_path=sample_parent_path,
        )
        if payload.get("media_type") == "any":
            payload["media_type"] = verified["media_type"]
        created = tmdb_regex_rules.create_rule(payload)
        created["verified_target"] = verified
        return api_response(created, 201)
    except TmdbRegexTargetUnavailable as exc:
        return api_error(str(exc), 503)
    except ValueError as exc:
        return api_error(str(exc), 400)


@router.post("/tmdb-regex-rules/preview")
def preview_tmdb_regex_rule_api(request: Request, data: dict | None = Body(default=None)):
    require_api_login(request)
    data = data or {}
    filename = str(data.get("filename") or "").strip()
    if not filename:
        return api_error("请输入样例文件名", 400)
    rule = data.get("rule")
    if not isinstance(rule, dict):
        return api_error("缺少待预览规则", 400)
    try:
        parent_path = str(data.get("parent_path") or "").strip()
        requested_type = str(data.get("media_type") or "").strip().lower()
        if requested_type not in {"movie", "tv"}:
            requested_type = extract_recognition_context(filename, parent_path).media_type
        preview = tmdb_regex_rules.preview_rule(
            rule, filename, parent_path, requested_type,
        )
        if preview["matched"]:
            preview["verified_target"] = _validate_tmdb_regex_target(
                rule, filename=filename, parent_path=parent_path, media_type_hint=requested_type,
            )
        return api_response(preview)
    except TmdbRegexTargetUnavailable as exc:
        return api_error(str(exc), 503)
    except ValueError as exc:
        return api_error(str(exc), 400)


@router.put("/tmdb-regex-rules/{rule_id}")
def update_tmdb_regex_rule_api(
    request: Request, rule_id: int = Path(...), data: dict | None = Body(default=None)
):
    require_api_login(request)
    try:
        if tmdb_regex_rules.get_rule(rule_id) is None:
            return api_error("TMDB 强制匹配规则不存在", 404)
        payload = dict(data or {})
        tmdb_regex_rules.preview_rule(payload, "", "", "movie")
        sample_filename = str(payload.pop("sample_filename", "") or "").strip()
        sample_parent_path = str(payload.pop("sample_parent_path", "") or "").strip()
        verified = _validate_tmdb_regex_target(
            payload, filename=sample_filename, parent_path=sample_parent_path,
        )
        if payload.get("media_type") == "any":
            payload["media_type"] = verified["media_type"]
        updated = tmdb_regex_rules.update_rule(rule_id, payload)
        updated["verified_target"] = verified
        return api_response(updated)
    except TmdbRegexTargetUnavailable as exc:
        return api_error(str(exc), 503)
    except ValueError as exc:
        status = 404 if "不存在" in str(exc) else 400
        return api_error(str(exc), status)


@router.delete("/tmdb-regex-rules/{rule_id}")
def delete_tmdb_regex_rule_api(request: Request, rule_id: int = Path(...)):
    require_api_login(request)
    try:
        return api_response({"success": tmdb_regex_rules.delete_rule(rule_id)})
    except ValueError as exc:
        return api_error(str(exc), 400)


@router.post("/gcid/export")
def export_gcid_manifest(request: Request, data: dict | None = Body(default=None)):
    """递归扫描光鸭目录并下载版本化 GCID 清单。只读操作。"""
    require_api_login(request)
    data = data or {}
    source_dir_id = str(data.get("source_dir_id") or "").strip()
    source_name = str(data.get("source_name") or "").strip()[:200]
    if not source_dir_id or len(source_dir_id) > 128:
        return api_error("请选择有效的源目录", 400)
    try:
        from app.clients.guangya import GuangYaClient

        client = GuangYaClient()
        if not client.logged_in:
            return api_error("光鸭未登录", 503)
        manifest = client.generate_gcid_json(source_dir_id, source_name)
        payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        safe_name = _safe_ascii_name(source_name or source_dir_id)[:80] or "guangya"
        date = datetime.now().strftime("%Y%m%d-%H%M%S")
        download_name = f"{safe_name}-gcid-{date}.json"
        return Response(
            content=payload,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{download_name}"',
                "Cache-Control": "no-cache, max-age=0",
            },
        )
    except ValueError as exc:
        return api_error(str(exc), 400)
    except Exception as exc:
        logger.error("GCID 清单导出失败 type=%s", type(exc).__name__)
        return api_error("GCID 清单导出失败", 502)


@router.post("/gcid/validate")
def validate_gcid_manifest(request: Request, data: dict | None = Body(default=None)):
    """校验上传的 JSON 清单；不执行任何云端写操作。"""
    require_api_login(request)
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > 20 * 1024 * 1024:
        return api_error("清单不能超过 20 MB", 413)
    try:
        return api_response(validate_manifest(data))
    except ManifestValidationError as exc:
        return api_response({"valid": False, "error": str(exc)}, 400)


def _gcid_owner_id(request: Request) -> str:
    return str(request.session.get("csrf_token") or "").strip()


def _gcid_content_too_large(request: Request) -> bool:
    try:
        return int(request.headers.get("content-length") or 0) > 20 * 1024 * 1024
    except (TypeError, ValueError):
        return True


def _require_gcid_confirmation(data: dict) -> Response | None:
    if str(data.get("confirm") or "") != "GCID":
        return api_error("请输入 GCID 确认导入操作", 400)
    return None


@router.post("/gcid/import/preview")
def preview_gcid_import(request: Request, data: dict | None = Body(default=None)):
    """只读解析 v2 清单并创建 owner/target/digest 绑定预览。"""
    require_api_login(request)
    if _gcid_content_too_large(request):
        return api_error("清单不能超过 20 MB", 413)
    data = data or {}
    if set(data) - {"manifest", "target_dir_id"}:
        return api_error("请求包含不允许的字段", 400)
    target_dir_id = str(data.get("target_dir_id") or "").strip()
    if not target_dir_id or len(target_dir_id) > 256:
        return api_error("请选择有效的目标目录", 400)
    try:
        return api_response(gcid_import.create_preview(
            data.get("manifest"),
            target_dir_id=target_dir_id,
            owner_id=_gcid_owner_id(request),
        ))
    except (ManifestValidationError, gcid_import.PreviewBindingError) as exc:
        return api_error(str(exc), 400)


@router.post("/gcid/import/run")
def run_gcid_import(request: Request, data: dict | None = Body(default=None)):
    """执行已确认预览；私有 importer 未配置时严格 503 fail closed。"""
    require_api_login(request)
    data = data or {}
    if set(data) - {
        "preview_id", "target_dir_id", "manifest_digest", "confirm", "operation_token"
    }:
        return api_error("请求包含不允许的字段", 400)
    confirmation_error = _require_gcid_confirmation(data)
    if confirmation_error:
        return confirmation_error
    try:
        task, replayed = gcid_import.run_preview(
            preview_id=str(data.get("preview_id") or ""),
            target_dir_id=str(data.get("target_dir_id") or ""),
            owner_id=_gcid_owner_id(request),
            manifest_digest=str(data.get("manifest_digest") or ""),
            operation_token=str(data.get("operation_token") or ""),
        )
        return api_response({"task": task, "replayed": replayed})
    except gcid_import.ImportCapabilityUnavailable as exc:
        return api_error(str(exc), 503)
    except gcid_import.PreviewBindingError as exc:
        return api_error(str(exc), 409)
    except gcid_import.OperationTokenConflict as exc:
        return api_error(str(exc), 409)
    except ValueError as exc:
        return api_error(str(exc), 400)


@router.post("/gcid/import/{task_id}/retry")
def retry_gcid_import(
    request: Request, task_id: int = Path(..., ge=1), data: dict | None = Body(default=None)
):
    """仅重试失败 item，原地保留已成功 item 的数据库 id。"""
    require_api_login(request)
    data = data or {}
    if set(data) - {"confirm", "operation_token"}:
        return api_error("请求包含不允许的字段", 400)
    confirmation_error = _require_gcid_confirmation(data)
    if confirmation_error:
        return confirmation_error
    try:
        task, replayed = gcid_import.retry_task(
            task_id, operation_token=str(data.get("operation_token") or "")
        )
        return api_response({"task": task, "replayed": replayed})
    except gcid_import.ImportCapabilityUnavailable as exc:
        return api_error(str(exc), 503)
    except gcid_import.ImportTaskNotFound as exc:
        return api_error(str(exc), 404)
    except ValueError as exc:
        return api_error(str(exc), 409 if "可重试" in str(exc) else 400)


@router.get("/gcid/import/tasks")
def list_gcid_import_tasks_api(
    request: Request, limit: int = Query(default=30, ge=1, le=200)
):
    require_api_login(request)
    return api_response({"tasks": gcid_import.list_tasks(limit)})


@router.get("/gcid/capabilities")
def gcid_capabilities(request: Request):
    require_api_login(request)
    capability = gcid_import.importer_capability()
    return api_response({
        "export": True,
        "validate": True,
        "cloud_only_import": capability["available"],
        "reason": capability["reason"],
    })


def _configured_test_url(base_url: str, suffix: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return ""
    parsed = urlparse(base)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return ""
    return base + suffix


def _proxy_test_targets() -> list[dict[str, str]]:
    targets = [
        {"key": "tmdb_api", "name": "TMDB API", "url": "https://api.themoviedb.org/3/configuration"},
        {"key": "tmdb_alt_api", "name": "TMDB 备用 API", "url": "https://api.tmdb.org/3/configuration"},
        {"key": "tmdb_web", "name": "TMDB 网站", "url": "https://www.themoviedb.org/"},
        {"key": "tmdb_image", "name": "TMDB 图片", "url": "https://image.tmdb.org/"},
        {"key": "tvdb_api", "name": "TheTVDB API", "url": "https://api.thetvdb.com/"},
        {"key": "fanart_api", "name": "Fanart API", "url": "https://webservice.fanart.tv/"},
        {"key": "telegram_api", "name": "Telegram API", "url": "https://api.telegram.org/"},
        {"key": "telegram_web", "name": "Telegram Web", "url": "https://telegram.me/"},
        {"key": "github", "name": "GitHub", "url": "https://github.com/"},
        {"key": "guangya_web", "name": "光鸭网站", "url": "https://www.guangyapan.com/"},
        {"key": "guangya_api", "name": "光鸭 API", "url": "https://api.guangyapan.com/"},
    ]
    configured = (
        ("jellyfin", "Jellyfin", config.get("JELLYFIN_URL", ""), "/System/Info/Public"),
        ("emby", "Emby", config.get("EMBY_URL", ""), "/System/Info/Public"),
        ("qb", "qBittorrent", config.get("QB_URL", ""), "/api/v2/app/version"),
    )
    for key, name, base_url, suffix in configured:
        url = _configured_test_url(base_url, suffix)
        if url:
            targets.append({"key": key, "name": name, "url": url})
    return targets


def _test_fixed_target(target: dict[str, str], proxies) -> dict:
    url = target["url"]
    parsed = urlparse(url)
    headers = {"User-Agent": "MediaFlux/1.0"}
    if target["key"] == "qb":
        api_key = config.get("QB_API_KEY", "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
    started = perf_counter()
    try:
        response = requests.get(
            url,
            headers=headers,
            proxies=proxies,
            timeout=10,
            allow_redirects=True,
            stream=True,
        )
        elapsed_ms = max(1, int((perf_counter() - started) * 1000))
        return {
            "key": target["key"],
            "name": target["name"],
            "host": parsed.hostname or "",
            "url": url,
            "ok": 200 <= response.status_code < 500,
            "status_code": response.status_code,
            "elapsed_ms": elapsed_ms,
            "error": "",
        }
    except requests.RequestException as exc:
        return {
            "key": target["key"],
            "name": target["name"],
            "host": parsed.hostname or "",
            "url": url,
            "ok": False,
            "status_code": 0,
            "elapsed_ms": max(1, int((perf_counter() - started) * 1000)),
            "error": str(exc),
        }


async def _fetch_ai_models(
    *, base_url: str, api_key: str, protocol: str, timeout_seconds: int
) -> list[str]:
    location = normalize_provider_location(base_url, https_only=True, public_only=True)
    headers = provider_headers(protocol, api_key, include_content_type=False)
    client = FixedHostHttpClient(
        allowed_hosts={location.host}, timeout_seconds=timeout_seconds,
        max_response_bytes=512 * 1024, max_redirects=0,
        user_agent="MediaFlux-AI-Models/1.0", pin_resolved_address=True,
    )
    try:
        response = await client.get(location.models_url, headers=headers, max_redirects=0)
        if response.status_code != 200:
            raise ValueError(f"Provider /models 返回 HTTP {response.status_code}")
        envelope = json.loads(response.text)
        raw_models = envelope.get("data") if isinstance(envelope, dict) else None
        if not isinstance(raw_models, list):
            raise ValueError("Provider /models 响应格式无效")
        models: list[str] = []
        for item in raw_models[:1000]:
            model_id = str(item.get("id") or "").strip() if isinstance(item, dict) else ""
            if model_id and len(model_id) <= 200 and model_id not in models:
                models.append(model_id)
        return sorted(models, key=str.casefold)
    finally:
        await client.aclose()


_AI_MODEL_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_AI_MODEL_TEST_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}
class _AIProviderTestFailure(RuntimeError):
    """不携带上游正文、URL 或凭据的模型测试失败信号。"""

    def __init__(
        self, kind: str, *, protocol: str = "", status_code: int = 0
    ) -> None:
        super().__init__(kind)
        self.kind = kind
        self.protocol = protocol
        self.status_code = int(status_code or 0)


def _ai_model_test_rate_key(request: Request) -> str:
    token = csrf_token(request)
    digest = hashlib.sha256(
        b"mediaflux-ai-model-test:v1\0" + token.encode("utf-8")
    ).hexdigest()
    return f"settings:ai-model-test:{digest}"


def _ai_model_test_timeout(raw_value: object) -> int:
    if isinstance(raw_value, bool):
        raise ValueError("请求超时必须是 2–30 秒的整数")
    if isinstance(raw_value, float) and not raw_value.is_integer():
        raise ValueError("请求超时必须是 2–30 秒的整数")
    if isinstance(raw_value, str) and not re.fullmatch(r"\d+", raw_value.strip()):
        raise ValueError("请求超时必须是 2–30 秒的整数")
    try:
        timeout_seconds = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("请求超时必须是 2–30 秒的整数") from exc
    if not 2 <= timeout_seconds <= 30:
        raise ValueError("请求超时必须在 2–30 秒之间")
    return timeout_seconds


def _ai_model_test_failure_message(failure: _AIProviderTestFailure) -> str:
    if failure.kind == "invalid_response":
        return "服务已连接，但模型未返回 Agent 所需的严格 JSON Schema；请检查协议或更换模型"
    if failure.status_code in {401, 403}:
        return "模型服务鉴权失败，请检查 API Key"
    if failure.status_code == 404:
        return "未找到接口或模型，请检查 API Base URL、协议和模型名称"
    if failure.status_code == 429:
        return "模型服务当前请求受限，请稍后再试"
    if failure.status_code in {400, 409, 415, 422}:
        return "模型服务拒绝了测试请求，请确认协议与模型支持严格 JSON Schema"
    if failure.status_code >= 500:
        return "模型服务暂时不可用，请稍后再试"
    return "模型连接测试失败，请检查地址、密钥、协议与模型名称"


async def _test_ai_provider(
    *,
    base_url: str,
    api_key: str,
    protocol: str,
    model: str,
    timeout_seconds: int,
) -> dict[str, object]:
    """验证结构化输出，并独立探测工具调用与流式输出能力。"""
    location = normalize_provider_location(base_url, https_only=True, public_only=True)
    protocols = protocol_attempts(protocol)
    client = FixedHostHttpClient(
        allowed_hosts={location.host},
        timeout_seconds=timeout_seconds,
        max_response_bytes=64 * 1024,
        max_redirects=0,
        user_agent="MediaFlux-AI-Model-Test/1.0",
        pin_resolved_address=True,
    )
    overall_started = perf_counter()
    probe_timeout = max(1.0, min(4.0, timeout_seconds / 3))

    async def _probe_tool_calling(attempted_protocol: str) -> bool:
        probe_name = "mediaflux_connectivity_probe"
        system_prompt = (
            "You are a capability probe. Call the supplied tool exactly once and do "
            "not answer with plain text."
        )
        capabilities = [{
            "name": probe_name,
            "description": "Confirm native tool-calling support.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        }]
        history = native_tool_initial_history(
            attempted_protocol,
            system_prompt=system_prompt,
            user_content="Call mediaflux_connectivity_probe now.",
        )
        response = await client.post_json(
            location.endpoint(attempted_protocol),
            json=native_tool_request_body(
                protocol=attempted_protocol,
                model=model,
                system_prompt=system_prompt,
                history=history,
                tools=native_tool_definitions(attempted_protocol, capabilities),
                max_tokens=64,
            ),
            headers=provider_headers(attempted_protocol, api_key),
            max_redirects=0,
        )
        if response.status_code != 200:
            return False
        envelope = json.loads(response.text)
        turn = parse_native_tool_turn(envelope, attempted_protocol)
        return (
            len(turn.tool_calls) == 1
            and turn.tool_calls[0].name == probe_name
            and turn.tool_calls[0].arguments == {}
        )

    async def _probe_streaming(attempted_protocol: str) -> bool:
        body = text_stream_request_body(
            protocol=attempted_protocol,
            model=model,
            system_prompt="You are a streaming capability probe. Reply with OK.",
            user_content="Reply with OK.",
            max_tokens=16,
        )
        async with client.stream_post_json(
            location.endpoint(attempted_protocol),
            json=body,
            headers=provider_headers(attempted_protocol, api_key, stream=True),
            max_redirects=0,
        ) as response:
            if response.status_code != 200:
                return False
            content_type = str(response.headers.get("content-type") or "").lower()
            if "text/event-stream" not in content_type:
                return False
            received_text = False
            async for delta in iter_provider_text_deltas(
                response.aiter_bytes(), protocol=attempted_protocol
            ):
                received_text = received_text or bool(str(delta).strip())
            return received_text

    async def _safe_capability_probe(probe) -> bool:
        try:
            return bool(await asyncio.wait_for(probe, timeout=probe_timeout))
        except (
            asyncio.TimeoutError,
            httpx.HTTPError,
            IndexerError,
            KeyError,
            IndexError,
            TypeError,
            ValueError,
        ):
            return False

    async def _request_protocols() -> dict[str, object]:
        for index, attempted_protocol in enumerate(protocols):
            attempt_started = perf_counter()
            body = structured_request_body(
                protocol=attempted_protocol,
                model=model,
                system_prompt=(
                    "You are a connectivity probe. Return only data matching the "
                    "required JSON schema."
                ),
                user_content='Return {"ok": true}.',
                schema_name="mediaflux_connectivity_probe",
                schema=_AI_MODEL_TEST_SCHEMA,
                max_tokens=32,
            )
            response = await client.post_json(
                location.endpoint(attempted_protocol),
                json=body,
                headers=provider_headers(attempted_protocol, api_key),
                max_redirects=0,
            )
            attempt_elapsed_ms = max(
                1, int((perf_counter() - attempt_started) * 1000)
            )
            if response.status_code != 200:
                can_fallback = (
                    index + 1 < len(protocols)
                    and is_protocol_fallback_error(
                        response.status_code,
                        response.text,
                        protocol=attempted_protocol,
                    )
                )
                if can_fallback:
                    logger.info(
                        "AI 模型测试 event=protocol_fallback protocol=%s "
                        "status_code=%s elapsed_ms=%s",
                        attempted_protocol,
                        response.status_code,
                        attempt_elapsed_ms,
                    )
                    continue
                logger.warning(
                    "AI 模型测试 event=upstream_status protocol=%s "
                    "status_code=%s elapsed_ms=%s",
                    attempted_protocol,
                    response.status_code,
                    attempt_elapsed_ms,
                )
                raise _AIProviderTestFailure(
                    "upstream_status",
                    protocol=attempted_protocol,
                    status_code=response.status_code,
                )
            try:
                envelope = json.loads(response.text)
                content = extract_output_text(envelope, attempted_protocol)
                if len(content) > 1024:
                    raise ValueError("AI 响应过长")
                parsed = json.loads(content)
            except (KeyError, IndexError, TypeError, ValueError):
                logger.warning(
                    "AI 模型测试 event=invalid_response protocol=%s "
                    "status_code=200 elapsed_ms=%s",
                    attempted_protocol,
                    attempt_elapsed_ms,
                )
                raise _AIProviderTestFailure(
                    "invalid_response", protocol=attempted_protocol, status_code=200
                ) from None
            if parsed != {"ok": True}:
                logger.warning(
                    "AI 模型测试 event=schema_mismatch protocol=%s "
                    "status_code=200 elapsed_ms=%s",
                    attempted_protocol,
                    attempt_elapsed_ms,
                )
                raise _AIProviderTestFailure(
                    "invalid_response", protocol=attempted_protocol, status_code=200
                )

            tool_calling = await _safe_capability_probe(
                _probe_tool_calling(attempted_protocol)
            )
            streaming = await _safe_capability_probe(
                _probe_streaming(attempted_protocol)
            )
            elapsed_ms = max(1, int((perf_counter() - overall_started) * 1000))
            logger.info(
                "AI 模型测试 event=success protocol=%s status_code=200 "
                "structured_output=true tool_calling=%s streaming=%s elapsed_ms=%s",
                attempted_protocol,
                str(tool_calling).lower(),
                str(streaming).lower(),
                elapsed_ms,
            )
            return {
                "ok": True,
                "protocol": attempted_protocol,
                "status_code": 200,
                "elapsed_ms": elapsed_ms,
                "capabilities": {
                    "structured_output": True,
                    "tool_calling": tool_calling,
                    "streaming": streaming,
                },
            }
        raise _AIProviderTestFailure("upstream_status")

    try:
        return await asyncio.wait_for(_request_protocols(), timeout=timeout_seconds)
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


@router.post("/ai/models")
def ai_models(request: Request, data: dict | None = Body(default=None)):
    """读取用户配置 Provider 的模型列表；不允许跳转或访问内网。"""
    require_api_login(request)
    data = data or {}
    unknown = sorted(set(data) - {"base_url", "api_key", "protocol"})
    if unknown:
        return api_error("模型读取包含不支持的参数", 400)
    base_url = str(data.get("base_url") or config.get("AGENT_LLM_API_URL", "") or "").strip()
    if not base_url:
        return api_error("请先填写 API Base URL", 400)
    api_key = str(data.get("api_key") or "").strip()
    if not api_key or api_key == "********":
        api_key = config.get("AGENT_LLM_API_KEY", "").strip()
    raw_protocol = str(
        data.get("protocol") or config.get("AGENT_LLM_PROTOCOL", "auto") or "auto"
    ).strip().lower().replace("-", "_")
    if raw_protocol not in PROTOCOLS:
        return api_error(f"接口协议仅支持 {SUPPORTED_PROTOCOLS_TEXT}", 400)
    protocol = resolve_protocol(raw_protocol, base_url)
    timeout_seconds = max(2, min(config.get_int("AGENT_LLM_TIMEOUT_SECONDS", 12), 30))
    try:
        models = run_awaitable_sync(_fetch_ai_models(
            base_url=base_url, api_key=api_key, protocol=protocol,
            timeout_seconds=timeout_seconds
        ))
    except (ValueError, IndexerError) as exc:
        return api_error(str(exc), 400)
    except Exception as exc:
        logger.warning("AI 模型列表读取失败 type=%s", type(exc).__name__)
        return api_error("模型列表读取失败，请检查地址、密钥与网络", 502)
    return api_response({"models": models, "protocol": protocol})


@router.post("/ai/test")
def ai_model_test(request: Request, data: dict | None = Body(default=None)):
    """使用当前表单草稿执行最小模型调用；不保存配置且不发送业务数据。"""
    require_api_login(request)
    data = data or {}
    unknown = sorted(
        set(data) - {"base_url", "api_key", "protocol", "model", "timeout_seconds"}
    )
    if unknown:
        return api_error("模型测试包含不支持的参数", 400)
    if not agent_rate_limiter.allow(
        _ai_model_test_rate_key(request), limit=6, window_seconds=60
    ):
        return api_error("模型测试过于频繁，请稍后再试", 429)

    base_url = str(
        data.get("base_url")
        if "base_url" in data
        else config.get("AGENT_LLM_API_URL", "")
    ).strip()
    if not base_url:
        return api_error("请先填写 API Base URL", 400)
    api_key = str(data.get("api_key") or "").strip()
    if not api_key or api_key == "********":
        api_key = config.get("AGENT_LLM_API_KEY", "").strip()
    raw_protocol = str(
        data.get("protocol")
        if "protocol" in data
        else config.get("AGENT_LLM_PROTOCOL", "auto")
    ).strip().lower().replace("-", "_")
    if raw_protocol not in PROTOCOLS:
        return api_error(f"接口协议仅支持 {SUPPORTED_PROTOCOLS_TEXT}", 400)
    protocol = resolve_protocol(raw_protocol, base_url)
    model = str(
        data.get("model")
        if "model" in data
        else config.get("AGENT_LLM_MODEL", "")
    ).strip()
    if not model:
        return api_error("请先填写模型名称", 400)
    if len(model) > 200 or _AI_MODEL_CONTROL_RE.search(model):
        return api_error("模型名称格式无效", 400)
    timeout_raw = (
        data.get("timeout_seconds")
        if "timeout_seconds" in data
        else config.get_int("AGENT_LLM_TIMEOUT_SECONDS", 12)
    )
    try:
        timeout_seconds = _ai_model_test_timeout(timeout_raw)
        normalize_provider_location(base_url, https_only=True, public_only=True)
        provider_headers("responses", api_key)
        result = run_awaitable_sync(
            _test_ai_provider(
                base_url=base_url,
                api_key=api_key,
                protocol=protocol,
                model=model,
                timeout_seconds=timeout_seconds,
            )
        )
    except _AIProviderTestFailure as exc:
        return api_error(_ai_model_test_failure_message(exc), 502)
    except (TimeoutError, httpx.TimeoutException):
        return api_error("模型连接测试超时，请检查网络或调高请求超时", 504)
    except ValueError as exc:
        return api_error(str(exc), 400)
    except (httpx.HTTPError, IndexerError) as exc:
        logger.warning("AI 模型测试传输失败 type=%s", type(exc).__name__)
        return api_error("无法连接模型服务，请检查地址、网络与代理设置", 502)
    except Exception as exc:
        logger.warning("AI 模型测试失败 type=%s", type(exc).__name__)
        return api_error("模型连接测试失败，请检查地址、密钥、协议与模型名称", 502)
    return api_response(result)


@router.post("/proxy/test")
def proxy_test(request: Request, data: dict | None = Body(default=None)):
    """并行测试服务端固定目标；不接受任意 URL，避免 SSRF。"""
    require_api_login(request)
    data = data or {}
    unknown = sorted(set(data) - {"use_proxy"})
    if unknown:
        return api_error("代理测试不接受自定义目标或 URL", 400)
    use_proxy = data.get("use_proxy", True) is not False
    proxies = None
    if use_proxy:
        proxy = config.get("PROXY_URL", "").strip()
        if proxy:
            proxy_url = proxy if proxy.startswith(("http://", "https://")) else f"http://{proxy}"
            parsed_proxy = urlparse(proxy_url)
            if not parsed_proxy.hostname or parsed_proxy.scheme not in ("http", "https"):
                return api_error("代理地址无效", 400)
            proxies = {"http": proxy_url, "https": proxy_url}

    targets = _proxy_test_targets()
    started = perf_counter()
    results: list[dict] = []
    workers = min(8, len(targets))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="proxy-test") as executor:
        futures = {executor.submit(_test_fixed_target, target, proxies): target for target in targets}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                target = futures[future]
                results.append({
                    "key": target["key"],
                    "name": target["name"],
                    "host": urlparse(target["url"]).hostname or "",
                    "url": target["url"],
                    "ok": False,
                    "status_code": 0,
                    "elapsed_ms": 0,
                    "error": str(exc),
                })
    order = {target["key"]: index for index, target in enumerate(targets)}
    results.sort(key=lambda item: order.get(item["key"], 999))
    passed = sum(1 for item in results if item["ok"])
    return api_response({
        "proxy_used": bool(proxies),
        "results": results,
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": len(results) - passed,
            "elapsed_ms": max(1, int((perf_counter() - started) * 1000)),
        },
    })
