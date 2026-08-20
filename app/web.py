"""FastAPI Web 公共层：模板、会话认证、JSON 响应与阻塞任务辅助。"""
from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


@pass_context
def relative_url_for(context: Any, name: str, **params: Any) -> str:
    """使用 FastAPI 路由参数生成同源相对 URL。

    模板内的导航、表单和静态资源均属于同源地址，返回相对 URL 可以避免
    反向代理错误组合 ``X-Forwarded-Proto=https`` 与裸后端端口时，把链接
    生成成 ``https://host:1258/...`` 并触发浏览器 TLS 协议错误。
    """
    request = context["request"]
    url = request.url_for(name, **params)
    relative = url.path
    if url.query:
        relative = f"{relative}?{url.query}"
    if url.fragment:
        relative = f"{relative}#{url.fragment}"
    return relative


templates.env.globals["url_for"] = relative_url_for


def csrf_token(request: Request) -> str:
    token = str(request.session.get("csrf_token") or "")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def render_template(request: Request, name: str, *, status_code: int = 200, **context: Any):
    token = csrf_token(request)
    context.setdefault("csrf_token", lambda: token)
    # 全局注入功能状态，避免各页面遗漏导致侧栏在刷新时闪现。
    from app.agent.feature_gate import is_agent_enabled

    context.setdefault("agent_enabled", is_agent_enabled())
    from app.build_metadata import PACKAGE_TYPE
    from app.version import BuildInfo

    context.setdefault("package_type", PACKAGE_TYPE)
    context.setdefault("app_version", BuildInfo.current().version)
    return templates.TemplateResponse(
        request=request,
        name=name,
        context=context,
        status_code=status_code,
    )


def require_api_login(request: Request) -> None:
    if not request.session.get("logged_in"):
        raise HTTPException(status_code=401, detail="unauthorized")


def require_page_login(request: Request):
    if not request.session.get("logged_in"):
        return RedirectResponse(request.url_for("auth.login"), status_code=302)
    return None


def api_error(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


def api_response(data: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status_code)
