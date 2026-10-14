# -*- coding: utf-8 -*-
"""報關發票(柬埔寨)數字化管理平台 — Web 入口。

启动::

    python -m uvicorn webapp.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from loguru import logger
from starlette.middleware.sessions import SessionMiddleware

from core.config import BASE_DIR, ensure_dirs
from .auth import LoginRequired, PermissionDenied, get_secret_key, hash_password, setup_logging
from .models import ROLE_ADMIN, User, SessionLocal, init_db
from .routers import admin_router, auth_router, detail_router, invoice_router
from .templates_setup import templates

setup_logging()
ensure_dirs()
init_db()

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="報關發票(柬埔寨)數字化管理平台", docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=get_secret_key(),
    same_site="lax",
    https_only=False,
    max_age=60 * 60 * 12,
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(auth_router.router)
app.include_router(invoice_router.router)
app.include_router(detail_router.router)
app.include_router(admin_router.router)


# ---- 异常处理 ----


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, exc: LoginRequired) -> Response:
    next_url = exc.next_url or str(request.url.path)
    return RedirectResponse(f"/login?next={next_url}", status_code=303)


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


# ---- 页面级兜底 ----


@app.get("/favicon.ico")
async def favicon() -> Response:
    return Response(status_code=204)


@app.get("/{path:path}")
async def page_not_found(request: Request, path: str) -> Response:
    if path.startswith(("api/", "static/", "admin/", "invoices", "details", "login",
                        "logout")):
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
    """首次启动创建默认管理员。"""
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
