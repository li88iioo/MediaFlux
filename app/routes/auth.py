"""FastAPI 认证与首次初始化路由。"""
from __future__ import annotations

import asyncio
import hmac

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.config import web_credentials
from app.logger import get_logger
from app.modules import first_run
from app.modules.login_wallpaper import get_login_wallpaper
from app.security import (
    clear_login_failures,
    clear_setup_failures,
    login_rate_limited,
    record_login_failure,
    record_setup_failure,
    setup_rate_limited,
)
from app.web import is_web_authenticated, render_template, web_session_credential

logger = get_logger(__name__)
router = APIRouter()

def _render_login(request: Request, *, status_code: int = 200, error: str = ""):
    return render_template(
        request,
        "login.html",
        status_code=status_code,
        error=error,
        login_wallpaper=get_login_wallpaper(),
        setup_mode=False,
    )


def render_setup(
    request: Request,
    *,
    status_code: int = 200,
    error: str = "",
    username: str = "",
):
    """复用登录布局渲染首启页；故意不请求 TMDB wallpaper。"""
    return render_template(
        request,
        "login.html",
        status_code=status_code,
        error=error,
        login_wallpaper=None,
        setup_mode=True,
        username=username,
    )


@router.get("/login", name="auth.login")
async def login_page(request: Request):
    if first_run.needs_initialization():
        return RedirectResponse("/setup", status_code=302)
    # 安装已提交但浏览器中断了原请求时，登录页仍可在同一服务内完成激活。
    activate = getattr(request.app.state, "activate_runtime", None)
    if activate is not None and not request.app.state.ready:
        await activate()
    if is_web_authenticated(request):
        return RedirectResponse("/", status_code=302)
    return await asyncio.to_thread(_render_login, request)


@router.post("/login", name="auth.login_submit")
async def login_submit(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
):
    if first_run.needs_initialization():
        return RedirectResponse("/setup", status_code=302)
    activate = getattr(request.app.state, "activate_runtime", None)
    if activate is not None and not request.app.state.ready:
        try:
            await activate()
        except Exception as exc:  # noqa: BLE001 - 未就绪时不得签发已登录会话。
            logger.error("登录前运行时启动失败 type=%s", type(exc).__name__)
            request.session.clear()
            return await asyncio.to_thread(
                _render_login, request, status_code=503,
                error="服务尚未就绪，请检查日志并重启服务后登录",
            )
    if is_web_authenticated(request):
        return RedirectResponse("/", status_code=302)
    identity = request.client.host if request.client else "unknown"
    if login_rate_limited(identity):
        return await asyncio.to_thread(
            _render_login, request,
            status_code=429,
            error="登录失败次数过多，请稍后再试",
        )
    username = username[:128]
    password = password[:512]
    ok_user, ok_pass = web_credentials()
    credentials_ready = bool(ok_user and ok_pass)
    valid_user = credentials_ready and hmac.compare_digest(
        username.encode("utf-8"), ok_user.encode("utf-8")
    )
    valid_password = credentials_ready and hmac.compare_digest(
        password.encode("utf-8"), ok_pass.encode("utf-8")
    )
    valid = valid_user and valid_password
    if valid:
        clear_login_failures(identity)
        request.session.clear()
        request.session["logged_in"] = web_session_credential((ok_user, ok_pass))
        logger.info(f"用户登录成功: {username}")
        return RedirectResponse("/", status_code=302)
    record_login_failure(identity)
    return await asyncio.to_thread(
        _render_login, request,
        status_code=401,
        error="用户名或密码错误",
    )


@router.get("/setup", name="auth.setup")
def setup_page(request: Request):
    if not first_run.needs_initialization():
        return RedirectResponse("/login", status_code=302)
    return render_setup(request, error=first_run.initialization_error())



@router.post("/setup", name="auth.setup_submit")
async def setup_submit(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
    password_confirm: str = Form(default=""),
):
    if not first_run.needs_initialization():
        return RedirectResponse("/login", status_code=302)

    identity = request.client.host if request.client else "unknown"
    if setup_rate_limited(identity):
        return render_setup(request, status_code=429, error="初始化失败次数过多，请稍后再试", username=username[:128])
    activate = getattr(request.app.state, "activate_runtime", None)
    if activate is None:
        return render_setup(request, status_code=503, error="服务尚未完成启动，请稍后重试")
    try:
        if password != password_confirm:
            raise ValueError("两次输入的密码不一致")
        await asyncio.to_thread(
            first_run.initialize_admin, username, password,
            secret_key=request.app.state.setup_secret_key,
        )
    except ValueError as exc:
        record_setup_failure(identity)
        return render_setup(request, status_code=400, error=str(exc), username=username[:128])
    except first_run.InitializationError as exc:
        if exc.initialized_by_competitor:
            return RedirectResponse("/login", status_code=303)
        record_setup_failure(identity)
        return render_setup(request, status_code=409, error=str(exc), username=username[:128])
    try:
        await activate()
    except Exception as exc:  # noqa: BLE001 - 服务启动失败不重放已提交的初始化。
        logger.error("首次安装后的运行时启动失败 type=%s", type(exc).__name__)
        return render_setup(request, status_code=503, error="管理员已保存，但服务启动失败，请检查日志并重启服务后登录")
    clear_setup_failures(identity)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.post("/logout", name="auth.logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)
