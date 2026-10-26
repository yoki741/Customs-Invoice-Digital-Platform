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

from core.config import SSO_MODE
from ..auth import (
    audit,
    get_user,
    hash_password,
    login_redirect_url,
    portal_logout_url,
    require_csrf,
    require_user,
    verify_password,
)
from ..models import ROLE_ADMIN, ROLE_TYPE1, ROLE_TYPE2, User, get_db
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
    next_url = _safe_next(next)
    # 已认证（可信代理身份或会话）直接回首页
    if user is not None:
        return RedirectResponse("/", status_code=303)
    if SSO_MODE:
        # 统一登录模式没有本地登录页，交给 oauth2-proxy 发起 Keycloak 登录
        return RedirectResponse(login_redirect_url(next_url), status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": next_url, "error": request.session.pop("flash_error", "")},
    )


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
    db: Session = Depends(get_db),
) -> object:
    if SSO_MODE:
        # 统一登录模式下不接受本地口令，避免绕过 Portal 的账号策略
        return RedirectResponse(login_redirect_url(_safe_next(next)), status_code=303)

    ip = request.client.host if request.client else "unknown"
    fail_key = f"{ip}|{username.strip()}"
    if _login_blocked(fail_key):
        request.session["flash_error"] = "登录失败次数过多，请 1 分钟后再试"
        logger.warning("登录限流拦截: {}", fail_key)
        return RedirectResponse(login_redirect_url(_safe_next(next)), status_code=303)

    name = username.strip()
    user: Optional[User] = db.query(User).filter(User.username == name).one_or_none()
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        _record_login_fail(fail_key)
        request.session["flash_error"] = "用户名或密码错误，或账号已停用"
        return RedirectResponse(login_redirect_url(_safe_next(next)), status_code=303)

    _clear_login_fails(fail_key)
    request.session.clear()
    request.session["uid"] = user.id
    audit(db, user, "login", f"{user.username} 登录（{user.role}）")
    return RedirectResponse(_safe_next(next) if next else _home_for(user), status_code=303)


@router.get("/logout")
async def logout(request: Request, db: Session = Depends(get_db)) -> object:
    """登出。

    统一登录模式下只清本地会话是不够的：下一个请求会被 Portal 重新注入身份
    （JIT 又把账号"登录"回来）。因此必须走 Portal 的完整登出链——
    oauth2-proxy 清 cookie → Keycloak 结束 SSO 会话 → 回首页。
    """
    uid = request.session.get("uid")
    if uid:
        user = db.get(User, uid)
        if user:
            audit(db, user, "logout", f"{user.username} 登出")
    request.session.clear()
    if SSO_MODE:
        return RedirectResponse(portal_logout_url(), status_code=302)
    return RedirectResponse("/login", status_code=303)


@router.get("/")
async def home(user: User = Depends(require_user)) -> object:
    return RedirectResponse(_home_for(user), status_code=303)


@router.get("/change-password")
async def change_password_page(
    request: Request,
    user: User = Depends(require_user),
) -> object:
    if SSO_MODE:
        return templates.TemplateResponse(
            request,
            "message.html",
            {
                "user": user,
                "level": "info",
                "title": "密码由统一登录管理",
                "message": (
                    "本平台已接入企业统一登录（Keycloak）。"
                    "如需修改密码，请在 Portal 首页右上角的账号菜单中操作，"
                    "或联系系统管理员重置。"
                ),
                "back": "/",
            },
        )
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
    if SSO_MODE:
        # 口令由 Keycloak 统一托管，本地改密会让两处不一致
        return RedirectResponse("/change-password", status_code=303)
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
