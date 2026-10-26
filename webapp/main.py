# -*- coding: utf-8 -*-
"""報關發票(柬埔寨)數字化管理平台 — Web 入口。

启动::

    # 本地开发（本地账号密码登录）
    python -m uvicorn webapp.main:app --host 127.0.0.1 --port 8000

    # 生产（DTD_nginx Portal 子路径挂载，身份由 Nginx 注入 Remote-* header）
    CUSTOMS_AUTH_MODE=sso uvicorn webapp.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from core.config import BASE_DIR, PORTAL_INTERNAL_URL, SEED_ADMIN, SSO_MODE, ensure_dirs
from .auth import (
    LoginRequired,
    PermissionDenied,
    get_secret_key,
    hash_password,
    login_redirect_url,
    setup_logging,
    wants_html,
)
from .middleware import TrustedProxyMiddleware
from .models import (
    ROLE_ADMIN,
    User,
    SessionLocal,
    init_db,
)
from .prefix import PrefixMiddleware
from .routers import admin_router, auth_router, detail_router, invoice_router
from .templates_setup import templates

setup_logging()
ensure_dirs()
init_db()

STATIC_DIR = Path(__file__).resolve().parent / "static"


class _StaticCacheMiddleware(BaseHTTPMiddleware):
    """给静态资源加缓存头（带 must-revalidate，改版后能及时刷新）。"""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers.setdefault(
                "Cache-Control", "public, max-age=86400, must-revalidate"
            )
        return response

app = FastAPI(title="報關發票(柬埔寨)數字化管理平台", docs_url=None, redoc_url=None)
# 中间件注册顺序说明：Starlette 后加入的在外层，因此 TrustedProxyMiddleware
# 最后注册，最先执行（伪造 Remote-* 的内网外请求在进入业务逻辑前就被拒绝）。
app.add_middleware(
    SessionMiddleware,
    secret_key=get_secret_key(),
    same_site="lax",
    # 生产经 Nginx 走 HTTPS；本地直连 http://127.0.0.1:8000 必须允许明文
    https_only=SSO_MODE,
    max_age=60 * 60 * 12,
)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(_StaticCacheMiddleware)
app.add_middleware(PrefixMiddleware)
app.add_middleware(TrustedProxyMiddleware)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(auth_router.router)
app.include_router(invoice_router.router)
app.include_router(detail_router.router)
app.include_router(admin_router.router)


# ---- 健康检查（供容器 healthcheck / Portal 监控使用，不鉴权） ----


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """返回服务健康状态，不触发认证。

    Returns:
        JSONResponse: 固定 200 与运行模式，便于运维核对。
    """
    return JSONResponse(
        {"status": "ok", "auth_mode": "sso" if SSO_MODE else "local"}
    )


# ---- 异常处理 ----


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, exc: LoginRequired) -> Response:
    """未认证处理。

    SSO 模式下 API 请求返回 401（前端可据此提示），页面请求跳 oauth2-proxy
    登录入口；本地模式统一跳本应用登录页。
    """
    next_url = exc.next_url or str(request.url.path)
    if SSO_MODE and not wants_html(request):
        return JSONResponse(
            {"code": 401, "message": "登录状态已失效，请重新登录"}, status_code=401
        )
    return RedirectResponse(login_redirect_url(next_url), status_code=303)



@app.exception_handler(PermissionDenied)
async def _permission_denied(request: Request, exc: PermissionDenied) -> Response:
    user_id = request.session.get("uid")
    return templates.TemplateResponse(
        request,
        "message.html",
        {
            "user": None,
            "level": "danger",
            "title": "无权访问",
            "message": exc.message,
            "back": "/",
            "user_id": user_id,
        },
        status_code=403,
    )


@app.exception_handler(Exception)
async def _server_error(request: Request, exc: Exception) -> Response:
    """兜底 500：记录日志，显示友好错误页。"""
    logger.exception("未处理异常: {} {}", request.method, request.url.path)
    return templates.TemplateResponse(
        request,
        "message.html",
        {
            "user": None,
            "level": "danger",
            "title": "系统出错了",
            "message": "系统处理请求时发生错误，请稍后重试；如持续出现请联系管理员。",
            "back": "/",
            "user_id": request.session.get("uid"),
        },
        status_code=500,
    )


# ---- 页面级兜底 ----


@app.get("/favicon.ico")
async def favicon() -> Response:
    return Response(status_code=204)


_RESERVED_PREFIXES = ("api", "static", "admin", "invoices", "details", "login", "logout")


@app.get("/{path:path}")
async def page_not_found(request: Request, path: str) -> Response:
    first_segment = path.split("/", 1)[0]
    if first_segment in _RESERVED_PREFIXES:
        return Response(status_code=404)
    return templates.TemplateResponse(
        request,
        "message.html",
        {
            "user": None,
            "level": "warning",
            "title": "页面不存在",
            "message": f"/{path}",
            "back": "/",
            "user_id": request.session.get("uid"),
        },
        status_code=404,
    )


def seed_admin() -> None:
    """首次启动创建默认管理员（仅本地开发，生产由统一登录 JIT 开通）。"""
    if not SEED_ADMIN:
        logger.info("已跳过默认管理员初始化（CUSTOMS_SEED_ADMIN=0）")
        return
    db = SessionLocal()

    try:
        if db.query(User).filter(User.role == ROLE_ADMIN).count() == 0:
            if db.query(User).count() == 0:
                admin = User(
                    username="admin",
                    password_hash=hash_password("admin123"),
                    role=ROLE_ADMIN,
                    display_name="系统管理员",
                    dept="关务",
                )
                db.add(admin)
                db.commit()
                logger.warning("已创建默认管理员 admin / admin123，请登录后立即修改")
    finally:
        db.close()


seed_admin()


@app.on_event("startup")
async def _startup() -> None:
    logger.info("平台启动：{}", BASE_DIR)
    if SSO_MODE:
        logger.info(
            "认证模式：sso（Nginx 注入 Remote-* header；登出经 Portal {}）",
            PORTAL_INTERNAL_URL,
        )
    else:
        logger.info("认证模式：local（本地账号密码登录，直连 8000 使用）")
    try:
        from .models import backup_db

        dest = backup_db()
        if dest:
            logger.info("已备份数据库：{}", dest)
    except Exception as exc:  # noqa: BLE001
        logger.warning("启动附加任务失败：{}", exc)

