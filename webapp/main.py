# -*- coding: utf-8 -*-
"""報關發票(柬埔寨)數字化管理平台 — Web 入口。

启动::

    python -m uvicorn webapp.main:app --host 127.0.0.1 --port 8000
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

from core.config import BASE_DIR, ensure_dirs
from .auth import LoginRequired, PermissionDenied, get_secret_key, hash_password, setup_logging
from .models import (
    ROLE_ADMIN,
    User,
    SessionLocal,
    init_db,
)
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

app = FastAPI(title="Customs Invoice (Cambodia) Digital Management Platform", docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=get_secret_key(),
    same_site="lax",
    https_only=False,
    max_age=60 * 60 * 12,
)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(_StaticCacheMiddleware)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(auth_router.router)
app.include_router(invoice_router.router)
app.include_router(detail_router.router)
app.include_router(admin_router.router)


# ---- 异常处理 ----


def _wants_json(request: Request) -> bool:
    """判断是否 fetch/XHR 调用接口（浏览器 Sec-Fetch-Dest: empty）。

    这类请求拿到跳转/HTML 会报 "Unexpected token '<'"，统一返回 JSON 错误；
    页面导航（表单提交、地址栏、<a>）不带该头，维持原有跳转/错误页行为。
    """
    return (
        request.url.path.startswith("/api/")
        and request.headers.get("sec-fetch-dest", "") == "empty"
    )


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, exc: LoginRequired) -> Response:
    next_url = exc.next_url or str(request.url.path)
    if _wants_json(request):
        return JSONResponse(
            {"ok": False, "error": "Session expired, please log in again",
             "login_required": True, "next": next_url},
            status_code=401,
        )
    return RedirectResponse(f"/login?next={next_url}", status_code=303)


@app.exception_handler(PermissionDenied)
async def _permission_denied(request: Request, exc: PermissionDenied) -> Response:
    if _wants_json(request):
        return JSONResponse({"ok": False, "error": exc.message}, status_code=403)
    user_id = request.session.get("uid")
    return templates.TemplateResponse(
        request,
        "message.html",
        {
            "user": None,
            "level": "danger",
            "title": "Access denied",
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
    if _wants_json(request):
        return JSONResponse(
            {"ok": False,
             "error": "An error occurred while processing the request, please try again later"},
            status_code=500,
        )
    return templates.TemplateResponse(
        request,
        "message.html",
        {
            "user": None,
            "level": "danger",
            "title": "Something went wrong",
            "message": "An error occurred while processing the request, please try again later; contact the administrator if it keeps happening.",
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
        if first_segment == "api":
            return JSONResponse(
                {"ok": False, "error": f"API not found: /{path}"}, status_code=404
            )
        return Response(status_code=404)
    return templates.TemplateResponse(
        request,
        "message.html",
        {
            "user": None,
            "level": "warning",
            "title": "Page not found",
            "message": f"/{path}",
            "back": "/",
            "user_id": request.session.get("uid"),
        },
        status_code=404,
    )


def seed_admin() -> None:
    """首次启动创建默认管理员。"""
    db = SessionLocal()
    try:
        if db.query(User).filter(User.role == ROLE_ADMIN).count() == 0:
            if db.query(User).count() == 0:
                admin = User(
                    username="admin",
                    password_hash=hash_password("admin123"),
                    role=ROLE_ADMIN,
                    display_name="System Admin",
                    dept="Customs",
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
    try:
        from .auth import get_header_auth_secret
        from .models import backup_db

        # 打印密钥指纹（前6位），便于运维核对 nginx 配置，不泄露全量
        secret = get_header_auth_secret()
        logger.info("Header 认证已启用，共享密钥指纹: {}…（nginx X-Internal-Token 需一致）", secret[:6])
        dest = backup_db()
        if dest:
            logger.info("已备份数据库：{}", dest)
    except Exception as exc:  # noqa: BLE001
        logger.warning("启动附加任务失败：{}", exc)
