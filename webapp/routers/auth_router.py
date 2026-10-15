# -*- coding: utf-8 -*-
"""登录 / 登出 / 首页跳转。"""

from __future__ import annotations

from typing import Optional
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..auth import (
    audit,
    get_user,
    hash_password,
    require_csrf,
    require_user,
    verify_password,
)
from ..models import ROLE_ADMIN, ROLE_TYPE1, ROLE_TYPE2, User, get_db
from ..templates_setup import templates

router = APIRouter(tags=["auth"])

HOME_BY_ROLE = {
    ROLE_ADMIN: "/admin",
    ROLE_TYPE1: "/invoices",
    ROLE_TYPE2: "/details",
}


def _safe_next(url: Optional[str]) -> str:
    """只允许站内相对路径，挡 //、/\\、/%2f%2f 等协议相对重定向。"""
    if not url:
        return "/"
    decoded = unquote(url)
    if not decoded.startswith("/"):
        return "/"
    if decoded.startswith("//") or decoded.startswith("/\\"):
        return "/"
    return decoded


def _home_for(user: User) -> str:
    return HOME_BY_ROLE.get(user.role, "/details")


@router.get("/login")
async def login_page(
    request: Request,
    next: str = "",
    user: Optional[User] = Depends(get_user),
) -> object:
    # 已通过 Header 透传或会话登录的用户直接进首页
    if user is not None:
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
async def home(user: User = Depends(require_user)) -> object:
    return RedirectResponse(_home_for(user), status_code=303)


@router.get("/change-password")
async def change_password_page(
    request: Request,
    user: User = Depends(require_user),
) -> object:
    return templates.TemplateResponse(
        request,
        "change_password.html",
        {
            "user": user,
            "error": request.session.pop("flash_error", ""),
            "ok": request.session.pop("flash_ok", ""),
        },
    )


@router.post("/change-password")
async def change_password(
    request: Request,
    old_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(""),
    csrf: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
) -> object:
    require_csrf(request, csrf)

    def fail(msg: str) -> object:
        request.session["flash_error"] = msg
        return RedirectResponse("/change-password", status_code=303)

    if not verify_password(old_password, user.password_hash):
        return fail("原密码错误")
    if len(new_password) < 6:
        return fail("新密码至少 6 位")
    if new_password != confirm_password:
        return fail("两次输入的新密码不一致")
    if new_password == old_password:
        return fail("新密码不能与原密码相同")

    user.password_hash = hash_password(new_password)
    db.commit()
    audit(db, user, "password.change", f"{user.username} 修改了自己的密码")
    request.session["flash_ok"] = "密码已修改"
    return RedirectResponse("/change-password", status_code=303)
