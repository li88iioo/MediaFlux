"""光鸭云盘 API 路由：登录态、短信两步登录、目录浏览。"""
from __future__ import annotations

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from app import config
from app.clients.guangya import GuangYaClient, TOKEN_EXPIRY_MAX, TOKEN_EXPIRY_MIN
from app.logger import get_logger
from app.modules.directory_scrape_errors import (
    DirectoryScrapePublicError,
    public_error_message,
)
from app.modules.scheduler import get_scheduler
from app.web import require_api_login

logger = get_logger(__name__)
router = APIRouter(prefix="/api/guangya")


TOKEN_STATUS_KEYS = (
    "has_access_token",
    "has_refresh_token",
    "expires_at",
    "valid",
    "access_token_masked",
    "refresh_token_masked",
)


def _safe_masked_indicator(value) -> str:
    text = str(value or "")
    if not text:
        return ""
    return "••••" if len(text) <= 4 else f"••••{text[-4:]}"


def _safe_expiry(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    if not TOKEN_EXPIRY_MIN <= parsed <= TOKEN_EXPIRY_MAX:
        return None
    return int(parsed) if parsed.is_integer() else parsed


def _safe_token_status(payload: dict) -> dict:
    """按固定白名单输出 Token 状态，隔离 SDK 或异常返回中的敏感字段。"""
    return {
        "has_access_token": bool(payload.get("has_access_token")),
        "has_refresh_token": bool(payload.get("has_refresh_token")),
        "expires_at": _safe_expiry(payload.get("expires_at")),
        "valid": bool(payload.get("valid")),
        "access_token_masked": _safe_masked_indicator(payload.get("access_token_masked")),
        "refresh_token_masked": _safe_masked_indicator(payload.get("refresh_token_masked")),
    }


def _capability_status() -> dict:
    sdk_available = True
    try:
        from guangyaclient import GuangyaClient as _SDKProbe  # noqa: F401
    except ImportError:
        sdk_available = False
    strm_error = get_scheduler().validate_config(auto_only=False)
    return {
        "sdk_available": sdk_available,
        "proxy_enabled": sdk_available,
        "strm_configured": not bool(strm_error),
        "strm_error": strm_error,
    }


def _clear_signed_url_caches() -> None:
    """凭证清除后立即废弃所有进程内光鸭签名直链。"""
    from app.modules.media_proxy import clear_signed_url_cache

    clear_signed_url_cache()


@router.get("/capabilities")
def capabilities(request: Request):
    """光鸭 SDK、反代与 STRM 配置状态。"""
    require_api_login(request)
    return _capability_status()


@router.post("/token/refresh")
def refresh_token(request: Request):
    """显式刷新 Token，并仅返回脱敏后的持久化状态。"""
    require_api_login(request)
    try:
        return _safe_token_status(GuangYaClient().refresh_now())
    except Exception as exc:
        logger.error(f"光鸭 token 显式刷新失败: {type(exc).__name__}")
        return JSONResponse({"error": "光鸭 token 刷新失败"}, status_code=409)


@router.post("/token/validate")
def validate_token(request: Request):
    """通过最小只读目录请求校验当前 Token。"""
    require_api_login(request)
    client = GuangYaClient()
    valid = client.validate()
    return _safe_token_status(client.token_status(valid=valid))


@router.post("/token/clear")
def clear_token(request: Request):
    """清除内存与磁盘 Token。"""
    require_api_login(request)
    status = _safe_token_status(GuangYaClient().clear_tokens())
    _clear_signed_url_caches()
    return status


@router.post("/send_sms")
def send_sms(request: Request, data: dict | None = Body(default=None)):
    """发送短信验证码（登录前）。init→send，captcha_token/verification_id 存 session。"""
    require_api_login(request)
    data = data or {}
    phone = (data.get("phone") or "").strip()
    if not phone:
        return JSONResponse({"error": "请输入手机号"}, status_code=400)

    c = GuangYaClient()
    try:
        init = c.login_init(phone)
        captcha_token = init.get("captcha_token", "") if isinstance(init, dict) else ""
        if not captcha_token:
            return JSONResponse({"error": "初始化失败", "detail": init}, status_code=500)
        send = c.send_sms(phone, captcha_token)
        verification_id = ""
        if isinstance(send, dict):
            verification_id = send.get("verification_id", "")
        request.session["gy_phone"] = phone
        request.session["gy_captcha_token"] = captcha_token
        request.session["gy_verification_id"] = verification_id
        return {
            "success": True,
            "verification_id": verification_id,
            "is_user": send.get("is_user") if isinstance(send, dict) else None,
            "expires_in": send.get("expires_in", 300) if isinstance(send, dict) else 300,
        }
    except Exception as e:
        logger.error("发送验证码失败 type=%s", type(e).__name__)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/login")
def login(request: Request, data: dict | None = Body(default=None)):
    """验证码登录。verify→signin，成功后 token 持久化。"""
    require_api_login(request)
    data = data or {}
    phone = (data.get("phone") or request.session.get("gy_phone") or "").strip()
    code = (data.get("code") or "").strip()
    captcha_token = data.get("captcha_token") or request.session.get("gy_captcha_token") or ""
    verification_id = data.get("verification_id") or request.session.get("gy_verification_id") or ""
    if not (phone and code and captcha_token and verification_id):
        return JSONResponse(
            {"error": "缺少参数（phone/code/verification_id/captcha_token）"},
            status_code=400,
        )

    c = GuangYaClient()
    try:
        ok = c.login(phone, code, verification_id=verification_id, captcha_token=captcha_token)
        for key in ("gy_phone", "gy_captcha_token", "gy_verification_id"):
            request.session.pop(key, None)
        return {"success": ok, "logged_in": ok}
    except Exception as e:
        logger.error("光鸭登录失败 type=%s", type(e).__name__)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/dirs")
def list_dirs(request: Request, parent_id: str = "0"):
    """目录浏览（给选择器用）。?parent_id=xxx，默认根目录。"""
    require_api_login(request)
    c = GuangYaClient()
    if not c.logged_in:
        return JSONResponse({"error": "光鸭未登录"}, status_code=503)
    try:
        from app.modules.organize import Organizer, OrganizeRules

        rules = OrganizeRules.from_config()
        video_exts = Organizer(client=c).video_exts(rules)
        files = c.list_dir(parent_id)
        return [
            {
                "file_id": f.file_id,
                "name": f.name,
                "is_dir": f.is_dir,
                "is_video": (
                    not f.is_dir
                    and "." in f.name
                    and f.name.rsplit(".", 1)[-1].lower() in video_exts
                ),
                "size": f.size,
                "created_at": f.created_at,
                "updated_at": f.updated_at,
                "mime_type": f.mime_type,
                "extension": (
                    f.extension
                    or (f.name.rsplit(".", 1)[-1].lower() if not f.is_dir and "." in f.name else "")
                ),
            }
            for f in files
        ]
    except Exception as exc:
        logger.error(f"光鸭目录读取失败: {type(exc).__name__}")
        return JSONResponse({"error": "光鸭目录读取失败"}, status_code=500)


@router.post("/delete-item")
def delete_item(request: Request, data: dict | None = Body(default=None)):
    """直接删除一个光鸭目录项；恢复由光鸭回收站负责。"""
    require_api_login(request)
    raw_id = (data or {}).get("file_id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        return JSONResponse({"error": "请选择需要删除的项目"}, status_code=400)
    file_id = raw_id.strip()
    if file_id == "0":
        return JSONResponse({"error": "不能删除光鸭根目录"}, status_code=400)

    client = GuangYaClient()
    try:
        if not client.logged_in:
            return JSONResponse({"error": "光鸭未登录"}, status_code=503)
        item = client.file_info(file_id)
        if item is None:
            return JSONResponse({"error": "项目不存在"}, status_code=404)
        client.delete([file_id])
        return {"ok": True, "file_id": file_id, "is_dir": bool(item.is_dir)}
    except Exception as exc:
        logger.error("光鸭删除项目失败: %s", type(exc).__name__)
        return JSONResponse({"error": "光鸭删除项目失败"}, status_code=500)


def _normalize_sources(data: dict) -> list[dict[str, str]]:
    from app.modules.organize_sources import normalize_organize_sources

    raw = data.get("source_dirs")
    if raw is None:
        source = str(data.get("source_dir_id", "") or "").strip()
        raw = [{"id": source, "name": "源目录"}] if source and source != "0" else []
    sources, _ = normalize_organize_sources(raw)
    return sources


# ===== 网盘整理 =====
def _build_rules(data: dict):
    """以已保存配置为基线构建一次整理使用的规则快照。

    Web 执行页只负责选择来源与目标；规则页保存的配置同时供 Web、TG、
    定时整理与本地媒体整理读取。保留 ``rules`` 覆盖参数用于兼容旧调用方，
    但只接受 OrganizeRules 已声明字段并按字段类型收敛。
    """
    from dataclasses import replace

    from app.modules.organize import OrganizeRules, enforce_fixed_organize_rules

    target_dir_id = str(
        data.get("target_dir_id")
        or config.get("GY_ORGANIZE_TARGET_DIR", "0")
        or "0"
    ).strip()
    rules = OrganizeRules.from_config(target_dir_id=target_dir_id)
    raw_overrides = data.get("rules")
    if raw_overrides is None:
        return rules
    if not isinstance(raw_overrides, dict):
        raise ValueError("rules 必须是 JSON 对象")

    fields = OrganizeRules.__dataclass_fields__
    bool_fields = {
        key for key, field in fields.items()
        if field.type == "bool" or isinstance(getattr(rules, key), bool)
    }
    int_fields = {
        key for key, field in fields.items()
        if field.type == "int" or (
            isinstance(getattr(rules, key), int)
            and not isinstance(getattr(rules, key), bool)
        )
    }
    overrides = {}
    for key, value in raw_overrides.items():
        if key not in fields or key in {
            "target_dir_id", "automatic_match_preset", "rename_enabled",
            "media_info_enabled", "media_probe_enabled", "media_probe_timeout",
            "movie_dir_template", "movie_template", "tv_template",
            "show_dir_template", "naming_scope",
        }:
            continue
        if key in bool_fields:
            if isinstance(value, bool):
                overrides[key] = value
            elif isinstance(value, (int, str)):
                text = str(value).strip().lower()
                if text in {"1", "true", "yes", "on"}:
                    overrides[key] = True
                elif text in {"0", "false", "no", "off", ""}:
                    overrides[key] = False
        elif key in int_fields:
            try:
                overrides[key] = int(value)
            except (TypeError, ValueError):
                continue
        elif value is not None:
            overrides[key] = str(value)
    return enforce_fixed_organize_rules(replace(rules, **overrides))


@router.post("/organize/preview")
def organize_preview(request: Request, data: dict | None = Body(default=None)):
    """预览多个源目录的整理计划（dry_run，不移动）。"""
    require_api_login(request)
    data = data or {}
    sources = _normalize_sources(data)
    if not sources:
        return JSONResponse({"error": "请选择至少一个源目录"}, status_code=400)
    try:
        rules = _build_rules(data)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    try:
        max_files = int(data.get("max_files", 100) or 100)
        if not 1 <= max_files <= 1000:
            raise ValueError("预览数量上限必须是 1-1000 的整数")
    except (TypeError, ValueError):
        return JSONResponse({"error": "预览数量上限必须是 1-1000 的整数"}, status_code=400)
    try:
        from app.modules.organize import Organizer

        all_plans = []
        aggregate: dict[str, int] = {}
        remaining = max_files
        protected_source_ids = {
            str(source.get("id") or "").strip()
            for source in sources
            if str(source.get("id") or "").strip()
        }
        for source in sources:
            if max_files and remaining <= 0:
                break
            organizer = Organizer()
            organizer._validate_target_outside_source(
                source["id"], rules.target_dir_id
            )
            plans, stats = organizer.organize(
                source["id"],
                rules,
                dry_run=True,
                max_files=remaining if max_files else 0,
                protected_source_ids=protected_source_ids,
            )
            for plan in plans:
                all_plans.append((source, plan))
            for key, value in stats.items():
                if isinstance(value, int):
                    aggregate[key] = aggregate.get(key, 0) + value
            if max_files:
                remaining -= int(stats.get("total", 0))
        return {
            "stats": aggregate,
            "plans": [
                {
                    "source_name": source["name"],
                    "action": p.action,
                    "original_name": p.original_name,
                    "original_path": p.original_path,
                    "size": p.size,
                    "title": p.match.title if p.match else "",
                    "year": p.match.year if p.match else "",
                    "tmdb_id": p.match.tmdb_id if p.match else "",
                    "confidence": p.match.confidence if p.match else 0,
                    "main_category": p.main_category,
                    "region": p.region,
                    "new_name": p.new_name,
                    "base_name": p.base_name,
                    "variant_label": p.variant_label,
                    "variant_suffix": p.variant_suffix,
                    "conflict_decision": p.conflict_decision,
                    "conflict_note": p.conflict_note,
                    "target_path": p.target_path,
                    "note": p.note,
                }
                for source, p in all_plans
            ],
        }
    except DirectoryScrapePublicError as exc:
        return JSONResponse(
            {"error": public_error_message(exc)},
            status_code=int(getattr(exc, "status_code", 409)),
        )
    except Exception as exc:
        logger.error("整理预览失败 type=%s", type(exc).__name__)
        return JSONResponse({"error": "整理预览失败，请稍后重试"}, status_code=500)


@router.post("/organize/run")
def organize_run(request: Request, data: dict | None = Body(default=None)):
    """后台启动多源整理任务，立即返回任务 ID。"""
    require_api_login(request)
    data = data or {}
    sources = _normalize_sources(data)
    target = str(data.get("target_dir_id", "0") or "0").strip()
    if not sources or target == "0":
        return JSONResponse({"error": "请选择源目录和目标目录"}, status_code=400)
    try:
        rules = _build_rules(data)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    from app.modules.organize_tasks import get_organize_manager

    result = get_organize_manager().start(sources, rules)
    return JSONResponse(result, status_code=202 if result.get("ok") else 409)


@router.get("/organize/status")
def organize_status(request: Request):
    require_api_login(request)
    from app.modules.organize_tasks import get_organize_manager

    return get_organize_manager().status()


@router.post("/organize/stop")
def organize_stop(request: Request):
    require_api_login(request)
    from app.modules.organize_tasks import get_organize_manager

    result = get_organize_manager().stop()
    return JSONResponse(result, status_code=202 if result.get("ok") else 409)


@router.post("/organize/clean-empty")
def organize_clean_empty(request: Request, data: dict | None = Body(default=None)):
    require_api_login(request)
    sources = _normalize_sources(data or {})
    if not sources:
        return JSONResponse({"error": "请选择至少一个源目录"}, status_code=400)
    from app.modules.organize_tasks import get_organize_manager

    result = get_organize_manager().clean_empty(sources)
    return JSONResponse(result, status_code=200 if result.get("ok") else 409)
