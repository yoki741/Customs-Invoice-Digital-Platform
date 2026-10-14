# -*- coding: utf-8 -*-
"""登录 / 登出 / 首页跳转。"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..auth import audit, verify_password
from ..models import ROLE_ADMIN, ROLE_TYPE1, ROLE_TYPE2, User, get_db
from ..templates_setup import templates

router = APIRouter(tags=["auth"])

HOME_BY_ROLE = {
    ROLE_ADMIN: "/admin",
    ROLE_TYPE1: "/invoices",
    ROLE_TYPE2: "/details",
}


def _safe_next(url: Optional[str]) -> str:
    if url and url.startswith("/") and not url.startswith("//"):
        return url
    return "/"


def _home_for(user: User) -> str:
    return HOME_BY_ROLE.get(user.role, "/details")


@router.get("/login")
async def login_page(request: Request, next: str = "") -> object:
    if request.session.get("uid"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": _safe_next(next), "error": request.session.pop("flash_error", "")},
    )


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
    db: Session = Depends(get_db),
) -> object:
    name = username.strip()
    user: Optional[User] = db.query(User).filter(User.username == name).one_or_none()
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        request.session["flash_error"] = "用户名或密码错误，或账号已停用"
        return RedirectResponse(
            f"/login?next={_safe_next(next)}", status_code=303
        )

    request.session.clear()
    request.session["uid"] = user.id
    audit(db, user, "login", f"{user.username} 登录（{user.role}）")
    return RedirectResponse(_safe_next(next) if next else _home_for(user), status_code=303)


@router.get("/logout")
async def logout(request: Request, db: Session = Depends(get_db)) -> object:
    uid = request.session.get("uid")
    if uid:
        user = db.get(User, uid)
        if user:
            audit(db, user, "logout", f"{user.username} 登出")
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/")
async def home(request: Request, db: Session = Depends(get_db)) -> object:
    uid = request.session.get("uid")
    if not uid:
        return RedirectResponse("/login", status_code=303)
    user = db.get(User, uid)
    if user is None or not user.is_active:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse(_home_for(user), status_code=303)
