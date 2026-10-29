# -*- coding: utf-8 -*-
"""登录 / 登出 / 首页跳转。"""

from __future__ import annotations

import time
from typing import Optional
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from loguru import logger
from sqlalchemy.orm import Session

from ..auth import (
    audit,
    get_user,
    hash_password,
    require_csrf,
    require_user,
    verify_password,
)
from ..models import ROLE_ADMIN, ROLE_TYPE1, ROLE_TYPE2, ROLE_TYPE3, User, get_db
from ..templates_setup import templates

router = APIRouter(tags=["auth"])

# 登录失败限流：(IP+用户名) -> (失败次数, 首次失败时间)
# 按 IP+用户名 组合限流，避免局域网共享出口 IP 时误伤其他同事
_LOGIN_FAILS: dict[str, tuple[int, float]] = {}
_FAIL_MAX = 5
_FAIL_WINDOW = 60.0


def _login_blocked(key: str) -> bool:
    """同一 IP 同一用户名连续失败 5 次后，60 秒内拒绝登录尝试。"""
    now = time.monotonic()
    item = _LOGIN_FAILS.get(key)
    if item and now - item[1] < _FAIL_WINDOW and item[0] >= _FAIL_MAX:
        return True
    return False


def _record_login_fail(key: str) -> None:
    now = time.monotonic()
    item = _LOGIN_FAILS.get(key)
    if item and now - item[1] < _FAIL_WINDOW:
        _LOGIN_FAILS[key] = (item[0] + 1, item[1])
    else:
        _LOGIN_FAILS[key] = (1, now)
    if len(_LOGIN_FAILS) > 2000:
        _LOGIN_FAILS.clear()


def _clear_login_fails(key: str) -> None:
    _LOGIN_FAILS.pop(key, None)

HOME_BY_ROLE = {
    ROLE_ADMIN: "/admin",
    ROLE_TYPE1: "/invoices",
    ROLE_TYPE2: "/details",
    ROLE_TYPE3: "/shipments",
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
    ip = request.client.host if request.client else "unknown"
    fail_key = f"{ip}|{username.strip()}"
    if _login_blocked(fail_key):
        request.session["flash_error"] = "Too many failed login attempts, please try again in 1 minute"
        logger.warning("登录限流拦截: {}", fail_key)
        return RedirectResponse(
            f"/login?next={_safe_next(next)}", status_code=303
        )

    name = username.strip()
    user: Optional[User] = db.query(User).filter(User.username == name).one_or_none()
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        _record_login_fail(fail_key)
        request.session["flash_error"] = "Incorrect username or password, or the account is disabled"
        return RedirectResponse(
            f"/login?next={_safe_next(next)}", status_code=303
        )

    _clear_login_fails(fail_key)
    request.session.clear()
    request.session["uid"] = user.id
    audit(db, user, "login", f"{user.username} logged in ({user.role})")
    return RedirectResponse(_safe_next(next) if next else _home_for(user), status_code=303)


@router.get("/logout")
async def logout(request: Request, db: Session = Depends(get_db)) -> object:
    uid = request.session.get("uid")
    if uid:
        user = db.get(User, uid)
        if user:
            audit(db, user, "logout", f"{user.username} logged out")
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
        return fail("Current password is incorrect")
    if len(new_password) < 6:
        return fail("New password must be at least 6 characters")
    if new_password != confirm_password:
        return fail("The two new passwords do not match")
    if new_password == old_password:
        return fail("New password cannot be the same as the current one")

    user.password_hash = hash_password(new_password)
    db.commit()
    audit(db, user, "password.change", f"{user.username} changed their own password")
    request.session["flash_ok"] = "Password changed"
    return RedirectResponse("/change-password", status_code=303)
