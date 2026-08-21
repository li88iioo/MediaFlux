"""FastAPI 应用工厂与 ASGI 启动入口。"""
from __future__ import annotations

import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import config, database
from app.modules import first_run, web_secret
from app.logger import get_logger
from app.security import SAFE_METHODS
from app.web import APP_DIR, csrf_token, render_template, templates

logger = get_logger(__name__)


_AGENT_REQUEST_BODY_LIMIT = 64 * 1024
_STATIC_COMPRESSIBLE_SUFFIXES = (".css", ".js", ".svg")
_STATIC_CACHE_CONTROL = "public, max-age=3600, must-revalidate"


def _append_vary(headers, value: str) -> None:
    current = headers.get("vary", "")
    values = [item.strip() for item in current.split(",") if item.strip()]
    if value.lower() not in {item.lower() for item in values}:
        values.append(value)
    headers["vary"] = ", ".join(values)


class CachedStaticFiles(StaticFiles):
    """为静态资源补充浏览器缓存策略，同时保留 Starlette 的 ETag/Range 行为。"""

    def file_response(self, full_path, stat_result, scope, status_code: int = 200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = _STATIC_CACHE_CONTROL
        return response


class StaticTextGZipMiddleware:
    """只压缩文本静态资源，避免干扰 API、媒体流与 Range 请求。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.gzip_app = GZipMiddleware(app, minimum_size=512, compresslevel=6)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_vary(message: Message) -> None:
            if message["type"] == "http.response.start":
                _append_vary(MutableHeaders(scope=message), "Accept-Encoding")
            await send(message)

        method = str(scope.get("method") or "GET").upper()
        path = str(scope.get("path") or "").lower()
        has_range = any(key.lower() == b"range" for key, _ in scope.get("headers", []))
        if method == "GET" and not has_range and path.endswith(_STATIC_COMPRESSIBLE_SUFFIXES):
            await self.gzip_app(scope, receive, send_with_vary)
            return
        await self.app(scope, receive, send_with_vary)


class AgentBodyLimitMiddleware:
    """在 FastAPI 解析 JSON 前限制 Agent 写请求的真实请求体大小。"""

    def __init__(self, app: ASGIApp, *, max_bytes: int = _AGENT_REQUEST_BODY_LIMIT) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or not str(scope.get("path") or "").startswith("/api/agent/")
            or str(scope.get("method") or "GET").upper() in SAFE_METHODS
        ):
            await self.app(scope, receive, send)
            return

        raw_lengths = [
            value.strip()
            for key, value in scope.get("headers", [])
            if key.lower() == b"content-length"
        ]
        if len(set(raw_lengths)) > 1:
            await self._reject_invalid_length(scope, receive, send)
            return
        raw_length = raw_lengths[0] if raw_lengths else b""
        declared_length: int | None = None
        if raw_length:
            if not raw_length.isdigit():
                await self._reject_invalid_length(scope, receive, send)
                return
            declared_length = int(raw_length)
            if declared_length > self.max_bytes:
                await self._reject_too_large(scope, receive, send)
                return

        buffered: list[Message] = []
        total = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_bytes:
                    await self._reject_too_large(scope, receive, send)
                    return
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                break

        if declared_length is not None and total != declared_length:
            await self._reject_invalid_length(scope, receive, send)
            return

        cursor = 0

        async def replay_receive() -> Message:
            nonlocal cursor
            if cursor < len(buffered):
                message = buffered[cursor]
                cursor += 1
                return message
            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject_too_large(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse({"error": "request body too large"}, status_code=413)
        await response(scope, receive, send)

    @staticmethod
    async def _reject_invalid_length(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse({"error": "invalid request body length"}, status_code=400)
        await response(scope, receive, send)


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if (
            request.method not in SAFE_METHODS
            and request.url.path.startswith("/api/")
            and not request.session.get("logged_in")
        ):
            return self._headers(
                request, JSONResponse({"error": "unauthorized"}, status_code=401)
            )
        if request.method not in SAFE_METHODS:
            if request.url.path == "/login":
                from app.modules.first_run import needs_initialization

                if needs_initialization():
                    return self._headers(
                        request,
                        RedirectResponse("/setup", status_code=302),
                    )
            expected = str(request.session.get("csrf_token") or "")
            supplied = str(request.headers.get("X-CSRF-Token") or "")
            if (
                not supplied
                and not request.url.path.startswith("/api/agent/")
                and request.headers.get("content-type", "").startswith(
                    "application/x-www-form-urlencoded"
                )
            ):
                body = await request.body()
                values = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
                supplied = str((values.get("csrf_token") or [""])[0])
            if not (expected and supplied and hmac.compare_digest(expected, supplied)):
                if request.url.path == "/login":
                    from app.modules.login_wallpaper import get_login_wallpaper

                    request.session.clear()
                    response = render_template(
                        request,
                        "login.html",
                        status_code=403,
                        error="登录会话已刷新，请重新输入凭证",
                        login_wallpaper=get_login_wallpaper(allow_refresh=False),
                        setup_mode=False,
                    )
                elif request.url.path == "/setup":
                    from app.routes.auth import render_setup

                    request.session.clear()
                    response = render_setup(
                        request,
                        status_code=403,
                        error="初始化会话已刷新，请重新提交表单",
                    )
                elif request.url.path.startswith("/api/"):
                    response = JSONResponse({"error": "CSRF token invalid"}, status_code=403)
                else:
                    response = PlainTextResponse("CSRF token invalid", status_code=403)
                return self._headers(request, response)
        response = await call_next(request)
        return self._headers(request, response)

    @staticmethod
    def _headers(request: Request, response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https://image.tmdb.org; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        content_type = str(response.headers.get("content-type") or "").lower()
        content_disposition = str(
            response.headers.get("content-disposition") or ""
        ).lower()
        dynamic_html = (
            content_type.startswith("text/html")
            and "attachment" not in content_disposition
        )
        if request.url.path.startswith("/api/") or dynamic_html:
            # 登录、首次初始化和已认证页面都携带会话/CSRF 动态状态，禁止浏览器
            # 或中间代理缓存后回放旧表单；静态资源仍沿用 StaticFiles 的缓存策略。
            response.headers.setdefault("Cache-Control", "no-store")
        return response


def _secret_key() -> str:
    fresh_install = first_run.needs_initialization()
    configured_secret = web_secret.configured_web_secret()
    production = config.get("APP_ENV", "development").strip().lower() == "production"
    try:
        secret_key = web_secret.get_web_secret()
    except web_secret.WebSecretUnavailable as exc:
        raise RuntimeError("生产模式必须配置 WEB_SECRET_KEY") from exc

    if fresh_install:
        # 首启（包括 production）必须可达 /setup；密钥会随初始化配置一起原子持久化。
        return secret_key
    if production and config.web_credentials()[1] == "123456":
        raise RuntimeError("生产模式禁止使用默认 Web 密码")
    if not configured_secret:
        config.set_and_save({"WEB_SECRET_KEY": secret_key})
        logger.warning("开发模式旧安装未配置 WEB_SECRET_KEY，已原子持久化新的会话密钥")
    return secret_key


def _resolve_bind_host(host: str | None) -> str:
    external_host = os.getenv("WEB_HOST", "")
    requested_host = host if host is not None else (external_host or None)
    return first_run.resolve_bind_host(requested_host)


def start_background_services() -> None:
    """启动进程内后台服务；重复调用安全。"""
    from app.modules.scheduler import get_scheduler
    from app.modules.rss_scheduler import get_rss_scheduler
    from app.modules.media_subscription_scheduler import get_media_subscription_scheduler
    from app.modules.organize_scheduler import get_organize_scheduler
    from app.modules.organize_tasks import get_organize_manager
    from app.modules.download_tracker import get_download_tracker
    from app.modules.agent_download_verification_scheduler import (
        get_download_library_verification_scheduler,
    )
    from app.modules.agent_library_patrol_scheduler import (
        get_agent_library_patrol_scheduler,
    )
    from app.modules.agent_jobs_scheduler import get_agent_jobs_scheduler
    from app.modules.local_media_scheduler import get_local_media_scheduler
    from app.modules.organize_confirmations import start_confirmation_dispatcher

    get_organize_manager().resume()
    start_confirmation_dispatcher()
    get_scheduler().start()
    get_rss_scheduler().start()
    get_media_subscription_scheduler().start()
    get_organize_scheduler().start()
    get_local_media_scheduler().start()
    get_download_tracker().start()
    from app.agent.feature_gate import is_agent_enabled

    if is_agent_enabled():
        get_download_library_verification_scheduler().start()
        get_agent_library_patrol_scheduler().start()
        get_agent_jobs_scheduler().start()
    if config.get("TG_BOT_TOKEN", "").strip() and config.get("TG_CHAT_ID", "").strip():
        from app.bot import start_bot

        start_bot()


def stop_background_services() -> bool:
    """停止任务生产者，再等待写任务在安全边界结束。"""
    subscription_workers_stopped = True
    organize_drained = True
    # 先关闭整理准入，避免迟到的 TG 回调或调度器在关机窗口提交新任务。
    from app.modules.organize_tasks import get_organize_manager

    organize_manager = get_organize_manager()
    organize_manager.begin_shutdown()

    # 再关闭会产生新任务的入口和轮询器。
    try:
        from app.bot import stop_bot

        stop_bot()
    except Exception as exc:
        logger.warning(f"停止 TG Bot 失败: {exc}")
    try:
        from app.modules.agent_jobs_scheduler import get_agent_jobs_scheduler

        get_agent_jobs_scheduler().stop()
    except Exception as exc:
        logger.warning("停止 Agent 持久化长任务调度器失败 type=%s", type(exc).__name__)
    try:
        from app.modules.agent_library_patrol_scheduler import (
            get_agent_library_patrol_scheduler,
        )

        get_agent_library_patrol_scheduler().stop()
    except Exception as exc:
        logger.warning("停止 Agent 全库缺集巡检调度器失败 type=%s", type(exc).__name__)
    try:
        from app.modules.agent_download_verification_scheduler import (
            get_download_library_verification_scheduler,
        )

        get_download_library_verification_scheduler().stop()
    except Exception as exc:
        logger.warning("停止 Agent 下载后媒体库复核调度器失败 type=%s", type(exc).__name__)
    try:
        from app.modules.download_tracker import get_download_tracker

        get_download_tracker().stop()
    except Exception as exc:
        logger.warning(f"停止下载跟踪器失败: {exc}")
    try:
        from app.modules.rss_scheduler import get_rss_scheduler

        get_rss_scheduler().stop()
    except Exception as exc:
        logger.warning(f"停止 RSS 调度器失败: {exc}")
    try:
        from app.modules.media_subscription_scheduler import get_media_subscription_scheduler

        subscription_workers_stopped = get_media_subscription_scheduler().stop()
    except Exception as exc:
        subscription_workers_stopped = False
        logger.warning("停止媒体订阅调度器失败 type=%s", type(exc).__name__)
    try:
        from app.modules.organize_scheduler import get_organize_scheduler

        get_organize_scheduler().stop()
    except Exception as exc:
        logger.warning(f"停止网盘整理调度器失败: {exc}")
    try:
        from app.modules.local_media_scheduler import get_local_media_scheduler

        get_local_media_scheduler().stop()
    except Exception as exc:
        logger.warning(f"停止本地媒体调度器失败: {exc}")

    # 停止 Telegram 候选队列消费者，保留 queued 项供下次启动恢复。
    try:
        from app.modules.organize_confirmations import stop_confirmation_dispatcher

        stop_confirmation_dispatcher()
    except Exception as exc:
        logger.warning(f"停止 Telegram 整理确认队列失败: {exc}")

    # 整理完成时可能排队触发 STRM，因此先收敛整理，再停止 STRM。
    try:
        organize_drained = bool(organize_manager.shutdown(timeout=30.0))
        if not organize_drained:
            logger.warning("停止网盘整理任务超时，保留依赖运行时直到进程退出")
    except Exception as exc:
        organize_drained = False
        logger.warning(f"停止网盘整理任务失败: {exc}")
    try:
        from app.modules.scheduler import get_scheduler

        get_scheduler().stop()
    except Exception as exc:
        logger.warning(f"停止 STRM 调度器失败: {exc}")
    return subscription_workers_stopped and organize_drained


def create_app(*, start_background: bool = False) -> FastAPI:
    secret_key = _secret_key()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        _app.state.ready = False
        from app.modules.backup import (
            recover_pending_restore,
            runtime_lifecycle_guard,
        )

        with runtime_lifecycle_guard(config.PATHS):
            recover_pending_restore(config.PATHS, lifecycle_lock_held=True)
            database.init_db()
            from app.modules.recognition_knowledge import ensure_seed_knowledge

            ensure_seed_knowledge()
            logger.info("数据库已初始化")
            from app.modules.media_proxy import get_media_proxy_manager

            proxy_manager = get_media_proxy_manager()
            _app.state.media_proxy_manager = proxy_manager
            from app.indexers.runtime import bind_indexer_event_loop

            indexer_event_loop = asyncio.get_running_loop()

            def _silence_proactor_connection_lost(
                loop: asyncio.AbstractEventLoop, context: dict
            ) -> None:
                exception = context.get("exception")
                if isinstance(exception, ConnectionResetError) or (
                    isinstance(exception, OSError)
                    and getattr(exception, "winerror", None) == 10054
                ):
                    return
                msg = str(context.get("message") or "")
                handle_str = str(context.get("handle") or "")
                if "_call_connection_lost" in handle_str or "_call_connection_lost" in msg:
                    if isinstance(exception, (ConnectionResetError, OSError)):
                        return
                loop.default_exception_handler(context)

            indexer_event_loop.set_exception_handler(_silence_proactor_connection_lost)
            bind_indexer_event_loop(indexer_event_loop)
            try:
                # 后台媒体订阅线程可能立即使用全局 Indexer；必须先绑定其唯一
                # 异步运行循环，避免启动窗口内创建跨循环的 HTTP 客户端。
                if start_background:
                    start_background_services()
                    await proxy_manager.start()
                _app.state.ready = True
                yield
            finally:
                _app.state.ready = False
                # 先停止会调用 discovery/indexer 的后台生产者及工作线程，
                # 再释放其运行时，避免关机窗口内出现资源已关闭但巡检仍在访问。
                from app.indexers.runtime import begin_indexer_shutdown

                begin_indexer_shutdown(indexer_event_loop)
                runtime_safe_to_close = True
                if start_background:
                    # 媒体订阅 worker 可能正等待提交到本生命周期循环的 Indexer
                    # 协程；若在主循环线程同步 join，会让双方互相等待直到停止超时。
                    try:
                        runtime_safe_to_close = await asyncio.to_thread(
                            stop_background_services
                        )
                    except Exception as exc:
                        runtime_safe_to_close = False
                        logger.warning(
                            "停止后台服务失败，保留运行时直到进程退出 type=%s",
                            type(exc).__name__,
                        )

                from app.discovery.service import shutdown_discovery_service
                from app.discovery.search import shutdown_discovery_search_service
                from app.indexers.runtime import (
                    shutdown_indexer_service,
                    unbind_indexer_event_loop,
                )

                try:
                    if runtime_safe_to_close:
                        shutdown_discovery_service()
                        shutdown_discovery_search_service()
                        await shutdown_indexer_service()
                    else:
                        logger.warning(
                            "媒体订阅检查尚未收敛，本次关机跳过 discovery/indexer runtime 销毁"
                        )
                    if start_background:
                        await proxy_manager.stop()
                finally:
                    if runtime_safe_to_close:
                        unbind_indexer_event_loop(indexer_event_loop)
                    else:
                        logger.warning(
                            "索引器运行时保持关闭门控，等待进程退出，避免存活 worker 跨事件循环复用客户端"
                        )

    app = FastAPI(title="MediaFlux", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.background_services_enabled = start_background
    app.state.ready = False
    app.add_middleware(AgentBodyLimitMiddleware)
    app.add_middleware(SecurityMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret_key,
        session_cookie="session",
        max_age=config.get_int("SESSION_LIFETIME_MINUTES", 720) * 60,
        same_site="lax",
        https_only=config.get_bool("SESSION_COOKIE_SECURE", False),
    )
    static_files = CachedStaticFiles(directory=str(APP_DIR / "static"))
    app.mount("/static", StaticTextGZipMiddleware(static_files), name="static")

    from app.routes.auth import router as auth_router
    from app.routes.pages import router as pages_router
    from app.routes.api import router as api_router
    from app.routes.proxy import router as proxy_router
    from app.routes.guangya_api import router as gy_router
    from app.routes.guangya_scrape_api import router as guangya_scrape_router
    from app.routes.rss_api import router as rss_router
    from app.routes.subscriptions_api import router as subscriptions_router
    from app.routes.strm_api import router as strm_router
    from app.routes.downloads_api import router as download_router
    from app.routes.tools_api import router as tools_router
    from app.routes.logs_api import router as logs_router
    from app.routes.offline_api import router as offline_router
    from app.routes.share_api import router as share_router
    from app.routes.media_image import router as media_image_router
    from app.routes.media_proxy_api import router as media_proxy_api_router
    from app.routes.discovery_api import router as discovery_api_router
    from app.routes.discovery_image import router as discovery_image_router
    from app.routes.indexers_api import router as indexers_api_router
    from app.routes.local_media_api import router as local_media_api_router
    from app.routes.agent_api import router as agent_api_router
    from app.routes.recognition_knowledge_api import router as recognition_knowledge_api_router

    for router in (
        auth_router, pages_router, api_router, proxy_router, gy_router,
        guangya_scrape_router,
        rss_router, subscriptions_router, strm_router, download_router,
        tools_router, logs_router, offline_router, share_router,
        media_image_router, media_proxy_api_router,
        discovery_api_router, discovery_image_router, indexers_api_router,
        local_media_api_router, agent_api_router, recognition_knowledge_api_router,
    ):
        app.include_router(router)

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return FileResponse(APP_DIR / "static" / "favicon.svg", media_type="image/svg+xml")

    @app.get("/healthz", name="healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz", name="readyz")
    async def readyz(request: Request):
        from app.version import BuildInfo
        from urllib.parse import urlsplit

        ready = bool(getattr(request.app.state, "ready", False))
        payload = {
            "service": "MediaFlux",
            "status": "ready" if ready else "starting",
            "version": BuildInfo.current().version,
        }
        response = JSONResponse(payload, status_code=200 if ready else 503)
        response.headers["Cache-Control"] = "no-store"
        origin = str(request.headers.get("origin") or "").strip()
        if origin:
            parsed = urlsplit(origin)
            if (
                parsed.scheme in {"http", "https"}
                and parsed.hostname
                and parsed.hostname == request.url.hostname
                and parsed.username is None
                and parsed.password is None
                and parsed.path in {"", "/"}
                and not parsed.query
                and not parsed.fragment
            ):
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Vary"] = "Origin"
        return response

    @app.exception_handler(HTTPException)
    async def handle_http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": exc.detail or "request failed"}, status_code=exc.status_code)
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception):
        # ServerErrorMiddleware 在调用此 handler 后仍会重新抛出异常，Uvicorn 会
        # 记录一份完整 traceback。应用侧只保留请求摘要，避免同一堆栈打印两遍。
        logger.error(
            "未处理请求异常 method=%s path=%s type=%s",
            request.method,
            request.url.path,
            type(exc).__name__,
        )
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "internal server error"}, status_code=500)
        return PlainTextResponse("Internal Server Error", status_code=500)

    return app


def run(*, host: str | None = None, port: int | None = None) -> None:
    """兼容 ASGI 直接启动，并始终保持单 worker。"""
    effective_host = _resolve_bind_host(host)
    effective_port = config.flask_port() if port is None else port
    logger.info(f"MediaFlux FastAPI 启动于 {effective_host}:{effective_port}")
    uvicorn.run(
        app,
        host=effective_host,
        port=effective_port,
        workers=1,
        reload=False,
        log_level="info",
        log_config=None,
        access_log=False,
    )


app = create_app(start_background=True)
