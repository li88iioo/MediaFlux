"""光鸭云盘 API 路由：登录态、短信两步登录、目录浏览。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import JSONResponse

from app import config
from app.clients.guangya import (
    TOKEN_EXPIRY_MAX,
    TOKEN_EXPIRY_MIN,
    GuangYaClient,
    GuangYaFile,
    close_guangya_client,
    read_guangya_credential_generation,
)
from app.logger import get_logger
from app.modules.directory_scrape_errors import (
    DirectoryScrapeConflictError,
    DirectoryScrapeGoneError,
    DirectoryScrapePublicError,
    DirectoryScrapeStateError,
    public_error_message,
)
from app.modules.scheduler import get_scheduler
from app.web import api_error, require_api_login

logger = get_logger(__name__)
router = APIRouter(prefix="/api/guangya")


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
    client = GuangYaClient()
    try:
        return _safe_token_status(client.refresh_now())
    except Exception as exc:
        logger.error(f"光鸭 token 显式刷新失败: {type(exc).__name__}")
        return JSONResponse({"error": "光鸭 token 刷新失败"}, status_code=409)
    finally:
        close_guangya_client(client)


@router.post("/token/validate")
def validate_token(request: Request):
    """只读校验 Access Token，保留布尔结果并返回具体状态。"""
    require_api_login(request)
    client = GuangYaClient()
    try:
        validation_status = client.access_token_validation_status()
        status = _safe_token_status(client.token_status(valid=validation_status == "valid"))
        if not status["has_access_token"]:
            status["valid"] = False
            validation_status = "invalid"
        return {**status, "validation_status": validation_status}
    finally:
        close_guangya_client(client)


@router.post("/token/clear")
def clear_token(request: Request):
    """清除内存与磁盘 Token。"""
    require_api_login(request)
    client = GuangYaClient()
    try:
        status = _safe_token_status(client.clear_tokens())
        _clear_signed_url_caches()
        return status
    finally:
        close_guangya_client(client)


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
    finally:
        close_guangya_client(c)


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
    finally:
        close_guangya_client(c)


def _browser_video_extensions() -> set[str]:
    from app.modules.organize import Organizer, OrganizeRules

    return Organizer.video_exts(OrganizeRules.from_config())


def _browser_item(file: GuangYaFile, video_exts: set[str]) -> dict:
    name_ext = file.name.rsplit(".", 1)[-1].lower() if not file.is_dir and "." in file.name else ""
    return {
        "file_id": file.file_id, "name": file.name, "is_dir": file.is_dir,
        "is_video": bool(name_ext) and name_ext in video_exts,
        "size": file.size, "etag": file.etag, "parent_id": file.parent_id,
        "created_at": file.created_at, "updated_at": file.updated_at,
        "mime_type": file.mime_type, "extension": file.extension or name_ext,
    }


@router.get("/search")
def search_files(
    request: Request, keyword: str = "", page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """按名称分页搜索整个光鸭账号下的文件和目录。"""
    require_api_login(request)
    keyword = keyword.strip()
    if not keyword:
        return api_error("请输入搜索关键词")
    client = GuangYaClient()
    try:
        if not client.logged_in:
            return api_error("光鸭未登录", 503)
        hits, has_more = client.search_files(keyword, page=page - 1, page_size=page_size)
        video_exts = _browser_video_extensions()
        return {
            "items": [dict(_browser_item(hit.file, video_exts), parent_name=hit.parent_name) for hit in hits],
            "page": page, "page_size": page_size, "has_more": has_more,
            "next_page": page + 1 if has_more else None,
        }
    except Exception as exc:
        logger.error("光鸭搜索失败 type=%s", type(exc).__name__)
        return api_error("光鸭搜索失败", 502)
    finally:
        close_guangya_client(client)


@router.get("/dirs")
def list_dirs(request: Request, parent_id: str = "0"):
    """目录浏览（给选择器用）。?parent_id=xxx，默认根目录。"""
    require_api_login(request)
    c = GuangYaClient()
    try:
        if not c.logged_in:
            return JSONResponse({"error": "光鸭未登录"}, status_code=503)
        video_exts = _browser_video_extensions()
        files = c.list_dir(parent_id)
        return [_browser_item(file, video_exts) for file in files]
    except Exception as exc:
        logger.error(f"光鸭目录读取失败: {type(exc).__name__}")
        return JSONResponse({"error": "光鸭目录读取失败"}, status_code=500)
    finally:
        close_guangya_client(c)


@router.post("/delete-item")
def delete_item(request: Request, data: dict | None = Body(default=None)):
    """把已确认的目录项删除提交到统一光鸭 Writer。"""
    require_api_login(request)
    payload = data or {}
    raw_id = payload.get("file_id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        return JSONResponse({"error": "请选择需要删除的项目"}, status_code=400)
    file_id = raw_id.strip()
    if file_id == "0":
        return JSONResponse({"error": "不能删除光鸭根目录"}, status_code=400)

    expected_name = str(payload.get("expected_name") or "").strip()
    expected_parent_id = str(payload.get("expected_parent_id") or "").strip()
    expected_etag = str(payload.get("expected_etag") or "").strip()
    expected_is_dir = payload.get("expected_is_dir")
    if expected_is_dir is not None and not isinstance(expected_is_dir, bool):
        return JSONResponse({"error": "删除项目类型无效"}, status_code=400)
    try:
        expected_updated_at = max(0, int(payload.get("expected_updated_at") or 0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "删除项目版本无效"}, status_code=400)

    def execute_delete() -> dict:
        from app.modules.download_staging_cleanup import (
            assert_guangya_download_delete_allowed,
        )
        from app.modules.organize_delete_audit import (
            DeleteCandidate,
            execute_recycle_bin_delete,
            record_blocked_delete,
        )

        client = GuangYaClient()
        try:
            if not client.logged_in:
                raise DirectoryScrapeStateError("光鸭未登录，请重新登录后再试")
            item = client.file_info(file_id)
            if item is None:
                raise DirectoryScrapeGoneError("项目已不存在，请刷新目录")
            if expected_name and item.name != expected_name:
                raise DirectoryScrapeConflictError("项目名称已变化，请刷新后重新确认")
            if expected_parent_id and item.parent_id != expected_parent_id:
                raise DirectoryScrapeConflictError("项目所在目录已变化，请刷新后重新确认")
            if expected_is_dir is not None and item.is_dir is not expected_is_dir:
                raise DirectoryScrapeConflictError("项目类型已变化，请刷新后重新确认")
            if expected_etag and (item.etag or "") != expected_etag:
                raise DirectoryScrapeConflictError("项目版本已变化，请刷新后重新确认")
            if expected_updated_at and (item.updated_at or 0) != expected_updated_at:
                raise DirectoryScrapeConflictError("项目更新时间已变化，请刷新后重新确认")
            candidate = DeleteCandidate(
                file_id=file_id,
                name=item.name,
                parent_id=item.parent_id,
                size=max(0, (item.size or 0)),
                gcid=(item.etag or ""),
            )
            try:
                assert_guangya_download_delete_allowed(client, item)
            except DirectoryScrapeConflictError as exc:
                record_blocked_delete(
                    trigger="web_direct_delete", reason=public_error_message(exc), candidate=candidate,
                )
                raise
            audit = execute_recycle_bin_delete(
                client,
                trigger="web_direct_delete",
                reason="用户已在 Web 目录浏览中确认移入回收站",
                candidate=candidate,
                safe_failure_message="光鸭删除项目失败，请稍后重试",
            )
            return {
                "ok": True,
                "file_id": file_id,
                "is_dir": item.is_dir,
                "audit_id": int(audit.get("audit_id") or 0),
                "stats": {"deleted": 1},
            }
        finally:
            close_guangya_client(client)

    from app.modules.organize_tasks import get_organize_manager

    result = get_organize_manager().start_operation(
        "删除光鸭目录项",
        expected_name or file_id,
        execute_delete,
        dedupe_key=f"guangya-direct-delete:{file_id}",
    )
    if not result.get("ok"):
        return JSONResponse(
            {"error": str(result.get("error") or "网盘整理任务正在运行")},
            status_code=409,
        )
    return JSONResponse(result, status_code=202)


@router.post("/file-change")
def file_change(request: Request, data: dict | None = None):
    """把浏览器的单项复制、移动或新建目录提交到光鸭 Writer。"""
    require_api_login(request)
    from app.modules.guangya_fs_change import (
        GuangYaFSChangeError,
        execute_browser_file_change,
        validate_fs_change_name,
    )
    from app.modules.organize_tasks import get_organize_manager

    payload = data or {}
    operation = payload.get("operation")
    labels = {"copy": "复制光鸭目录项", "move": "移动光鸭目录项", "mkdir": "新建光鸭目录"}
    if operation not in tuple(labels):
        return JSONResponse({"error": "请选择复制、移动或新建目录"}, status_code=400)
    target_dir_id = payload.get("target_dir_id")
    if not isinstance(target_dir_id, str) or not target_dir_id.strip():
        return JSONResponse({"error": "请选择目标目录"}, status_code=400)
    target_dir_id = target_dir_id.strip()
    file_id = expected_parent_id = expected_name = name = ""
    if operation == "mkdir":
        try:
            name = validate_fs_change_name(payload.get("name"))
        except GuangYaFSChangeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    else:
        raw_file_id = payload.get("file_id")
        file_id = raw_file_id.strip() if isinstance(raw_file_id, str) else ""
        if file_id in {"", "0"}:
            return JSONResponse({"error": "请选择具体文件或目录作为源项目"}, status_code=400)
        if file_id == target_dir_id:
            return JSONResponse({"error": "不能把项目复制或移动到自身"}, status_code=400)
        expected_parent_id = payload.get("expected_parent_id")
        expected_name = payload.get("expected_name")
        if (
            not isinstance(expected_parent_id, str) or not expected_parent_id.strip()
            or not isinstance(expected_name, str) or not expected_name.strip()
        ):
            return JSONResponse({"error": "缺少源项目的父目录或名称，请刷新后重试"}, status_code=400)
        expected_parent_id = expected_parent_id.strip()

    generation = read_guangya_credential_generation()

    def execute() -> dict:
        client = GuangYaClient()
        try:
            if client.credential_generation != generation:
                raise DirectoryScrapeStateError("光鸭登录已变化，请刷新目录后重试")
            return execute_browser_file_change(
                client, operation, target_dir_id=target_dir_id, name=name,
                file_id=file_id, expected_parent_id=expected_parent_id, expected_name=expected_name,
            )
        finally:
            close_guangya_client(client)

    identity = json.dumps([
        generation, operation, target_dir_id, file_id, expected_parent_id, expected_name, name,
    ])
    result = get_organize_manager().start_operation(
        labels[operation], name or expected_name, execute,
        dedupe_key=f"guangya-browser-file-change:{identity}",
    )
    return JSONResponse(result, status_code=202 if result.get("ok") else 409)


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
    """从已保存配置构建一次整理规则快照。

    Web 执行入口只选择来源与目标；规则唯一由设置页持久化，禁止请求体
    临时覆盖，确保 Web、Telegram、定时任务与本地媒体读取同一权威配置。
    """
    from app.modules.organize import OrganizeRules

    # 旧版页面会固定携带 ``rules: {}``。空对象没有覆盖语义，可以安全
    # 视作未提供；任何非空值仍然拒绝，避免重新形成请求级第二套规则权威。
    request_rules = data.get("rules")
    if request_rules not in (None, {}):
        raise ValueError("不支持请求级 rules 覆盖，请先在整理规则页保存配置")
    target_dir_id = str(
        data.get("target_dir_id")
        or config.get("GY_ORGANIZE_TARGET_DIR", "0")
        or "0"
    ).strip()
    return OrganizeRules.from_config(target_dir_id=target_dir_id)


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
        from app.modules.organize_service import preview_sources
        result = preview_sources(sources, rules, max_files=max_files)
        return {"stats": result["stats"], "plans": [
            {key: value for key, value in plan.items() if key != "source_id"}
            for plan in result["plans"]]}
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
def organize_status(request: Request, task_id: str = "", task_ids: str = ""):
    require_api_login(request)
    from app.modules.organize_tasks import get_organize_manager

    manager = get_organize_manager()
    web_owner = str(request.session.get("csrf_token") or "")

    def receipt(identifier: str) -> dict | None:
        task = manager.task_result(identifier, web_owner=web_owner)
        if not task or not task.get("operation"):
            return None
        fields = ("id", "status", "message", "error", "result")
        if task.get("durable"):
            fields += ("queue_position",)
        return {key: task.get(key) for key in fields}

    if task_ids:
        identifiers = dict.fromkeys(value for value in task_ids.split(",") if value)
        if len(identifiers) > 32:
            return JSONResponse({"error": "一次最多查询 32 个整理任务"}, status_code=400)
        return {"tasks": [task for identifier in identifiers if (task := receipt(identifier)) is not None]}
    if not task_id:
        return manager.status()
    task = receipt(task_id)
    if task is None:
        return JSONResponse({"error": "未找到该整理操作，请刷新日志核对结果"}, status_code=404)
    return task


@router.post("/organize/stop")
def organize_stop(request: Request, data: dict | None = Body(default=None)):
    require_api_login(request)
    from app.modules.organize_service import stop

    expected_task_id = ""
    if data and "expected_task_id" in data:
        value = data["expected_task_id"]
        if not isinstance(value, str) or not value.strip():
            return JSONResponse({"error": "缺少有效的整理任务 ID"}, status_code=400)
        expected_task_id = value.strip()
    result = stop(expected_task_id=expected_task_id)
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
