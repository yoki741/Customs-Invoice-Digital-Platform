# -*- coding: utf-8 -*-
"""认证、权限依赖与日志。

支持两种认证来源（由 ``CUSTOMS_AUTH_MODE`` 选择，见 core/config.py）：

1. ``sso``（生产，经 DTD_nginx Portal）：Nginx + oauth2-proxy + Portal
   Authorizer 认证后注入 ``Remote-Subject`` / ``Remote-User`` /
   ``Remote-Groups`` / ``Remote-Display-Name``；首次访问自动建号（JIT）。
   角色由 Keycloak 组决定（``CUSTOMS_ROLE_GROUPS``，默认
   ``/admin`` 与 ``/customs-invoice-admin`` → admin、
   ``/customs-invoice-type1`` → type1、``/apps/customs-invoice`` → type2），
   命中即同步进 ``users.role``；未命中任何组时沿用库内角色（不降级），
   新账号则用 ``CUSTOMS_DEFAULT_ROLE``（默认 type2）。本模式下不提供
   账号密码登录，登出交由 Portal 统一单点登出。
2. ``local``（开发 / 直连 8000）：用户名密码 + session cookie。

无论哪种模式，``users`` 表都只承载业务档案（业务角色、显示名、部门、停用开关），
不做统一身份源。
"""

from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlencode

import secrets

import bcrypt
from fastapi import Depends, Form, HTTPException, Request
from loguru import logger
from sqlalchemy.orm import Session

from core.config import (
    DEFAULT_ROLE,
    LOG_DIR,
    OAUTH2_SIGNOUT_URL,
    PORTAL_LOGOUT_API,
    ROLE_SYNC,
    SECRET_KEY_PATH,
    SSO_LOGIN_URL,
    SSO_MODE,
    ensure_dirs,
    resolve_role_from_groups,
)
from .models import ROLE_ADMIN, ROLE_TYPE1, ROLE_TYPE2, User, get_db

ROLE_ALL = (ROLE_ADMIN, ROLE_TYPE1, ROLE_TYPE2)

# Remote-User 允许字符（防日志注入、防伪造成非法用户名）
_REMOTE_USER_RE = re.compile(r"^[\w.\-@一-鿿]{1,64}$")



class LoginRequired(Exception):
    """未登录，跳转登录页。"""

    def __init__(self, next_url: str = "/") -> None:
        self.next_url = next_url
        super().__init__("login required")


class PermissionDenied(Exception):
    """已登录但无权访问。"""

    def __init__(self, message: str = "无权访问") -> None:
        self.message = message
        super().__init__(message)


# ---- 密码 ----


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---- CSRF ----


def ensure_csrf(request: Request) -> str:
    """确保会话里有 CSRF token，返回它（模板/表单注入用）。"""
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def require_csrf(request: Request, csrf: str = Form("")) -> None:
    """FastAPI 依赖：校验 POST 表单里的 csrf 字段。"""
    expected = request.session.get("csrf")
    if not expected or not csrf or not secrets.compare_digest(expected, csrf):
        raise HTTPException(status_code=403, detail="CSRF token 无效，请刷新页面重试")


# ---- 日志 ----


def setup_logging() -> None:
    """loguru 输出到控制台 + logs/webapp.log。"""
    ensure_dirs()
    logger.remove()
    logger.add(
        lambda msg: print(msg, end=""),
        level="INFO",
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level:<7}</level> | {message}",
    )
    logger.add(
        Path(LOG_DIR) / "webapp.log",
        level="DEBUG",
        rotation="10 MB",
        retention="30 days",
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<7} | {module}:{line} | {message}",
    )


def get_secret_key() -> str:
    """会话签名密钥，首次运行自动生成并持久化。"""
    ensure_dirs()
    if SECRET_KEY_PATH.exists():
        return SECRET_KEY_PATH.read_text(encoding="utf-8").strip()
    key = bcrypt.gensalt().decode("utf-8")
    SECRET_KEY_PATH.write_text(key, encoding="utf-8")
    return key


# ---- 登录入口 / 登出（与 Portal 对接） ----


def wants_html(request: Request) -> bool:
    """判断请求是否期望 HTML（浏览器导航）而不是 API JSON。

    Args:
        request (Request): 当前请求。

    Returns:
        bool: 浏览器页面请求返回 True。
    """
    if request.url.path.startswith("/api/"):
        return False
    return "text/html" in request.headers.get("accept", "")


def login_redirect_url(next_url: str = "") -> str:
    """生成未认证时的跳转地址。

    Args:
        next_url (str): 登录后要回到的应用内路径（不含子路径前缀）。

    Returns:
        str: SSO 模式返回 oauth2-proxy 登录入口；本地模式返回应用登录页。
    """
    target = next_url if next_url.startswith("/") else "/"
    if SSO_MODE:
        # /oauth2/ 属于 Portal 根路径，PrefixMiddleware 不会再补子路径前缀
        return f"{SSO_LOGIN_URL}?{urlencode({'rd': target})}"
    return f"/login?{urlencode({'next': target})}"


def portal_logout_url() -> str:
    """向 Portal 申请完整登出链（oauth2-proxy → Keycloak → 首页）。

    服务端直接调用 Portal 的 ``POST /api/auth/logout/``：该请求不带会话
    Cookie，Portal 按匿名请求处理并返回可用的 redirect_url，因此不需要
    在本平台重复维护 Keycloak 地址与 client_id。Portal 不可用时回落到
    oauth2-proxy 的 sign_out。

    Returns:
        str: 浏览器应跳转的登出地址。
    """
    try:
        # 地址为内部固定配置，不接收用户输入。
        logout_request = urllib.request.Request(  # noqa: S310
            PORTAL_LOGOUT_API, data=b"", method="POST"
        )
        with urllib.request.urlopen(logout_request, timeout=3) as response:  # noqa: S310
            payload = json.loads(response.read(4096).decode("utf-8"))
        redirect_url = str(payload.get("redirect_url") or "").strip()
        if redirect_url.startswith(("http://", "https://", "/")):
            return redirect_url
    except Exception as exc:  # noqa: BLE001
        logger.warning("获取 Portal 登出链失败，改用 oauth2-proxy 兜底: {}", exc)
    return OAUTH2_SIGNOUT_URL


# ---- 可信代理身份（Remote-* header） ----


def _resolve_role(groups: frozenset[str] | set[str]) -> tuple[str, str]:
    """按统一登录组映射解析本平台角色。

    ``ROLE_GROUP_MAP`` 已按权限从高到低排列（admin > type1 > type2），
    因此第一个命中的组即为最高权限角色。组值兼容三种写法
    （``/admin``、``admin``、嵌套组叶子名 ``customs-invoice``），
    因为现网 Keycloak 的 ``groups`` mapper 配置为 ``full.path=false``。

    Args:
        groups (set[str]): ``Remote-Groups`` 解析出的组值集合。

    Returns:
        tuple[str, str]: ``(角色, 命中的组路径)``；未命中任何组时返回
        ``("", "")``，表示本次身份不携带角色决定，交由调用方沿用库内角色。
    """
    return resolve_role_from_groups(groups)


def _remote_user(request: Request, db: Session) -> Optional[User]:
    """解析 Nginx 注入的 Remote-* 身份，必要时 JIT 开通账号。

    返回 None 表示本次请求没有可信身份（无 header / 用户名非法 / 已停用），
    由调用方回落到本地会话认证。

    Args:
        request (Request): 当前请求。
        db (Session): 数据库会话。

    Returns:
        Optional[User]: 匹配或新建的用户对象。
    """
    subject = request.headers.get("Remote-Subject", "").strip()
    username = request.headers.get("Remote-User", "").strip()
    if not username or not _REMOTE_USER_RE.match(username):
        if subject or username:
            logger.warning(
                "可信代理身份缺失或非法: subject={!r} user={!r}", subject, username
            )
        return None

    groups = {
        item.strip()
        for item in request.headers.get("Remote-Groups", "").split(",")
        if item.strip()
    }
    display_name = (
        request.headers.get("Remote-Display-Name", "")
        or request.headers.get("Remote-Name", "")
    ).strip()

    # 统一登录组是角色权威源：命中哪一组就是哪一类账号。
    group_role, matched_group = _resolve_role(groups)

    user = db.query(User).filter(User.username == username).one_or_none()
    if user is None:
        # JIT 开通：命中组映射则用命中角色，否则用默认角色（type2，仅查看明细）
        role = group_role or DEFAULT_ROLE
        user = User(
            username=username,
            password_hash=hash_password(secrets.token_urlsafe(16)),
            role=role,
            display_name=display_name[:64] or username,
        )
        db.add(user)
        db.commit()
        audit(
            db,
            user,
            "jit.provision",
            f"统一登录自动开通账号（角色 {role}，"
            f"来源 {matched_group or '默认'}，subject {subject or '-'}）",
        )
        logger.info(
            "统一登录 JIT 开通账号: {} ({}, 组 {})",
            username,
            role,
            matched_group or "默认",
        )
    elif ROLE_SYNC and group_role and user.role != group_role:
        # 组映射命中即同步：Keycloak 改组成员后刷新即生效，无需进本平台后台。
        old_role = user.role
        user.role = group_role
        db.commit()
        audit(
            db,
            user,
            "role.sync",
            f"统一登录组 {matched_group} 决定角色: {old_role} -> {group_role}",
        )
        logger.info(
            "统一登录组同步角色: {} {} -> {}（组 {}）",
            username,
            old_role,
            group_role,
            matched_group,
        )
    if not user.is_active:
        logger.warning("统一登录用户已被停用: {}", username)
        return None
    return user



# ---- 权限依赖 ----


def get_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    """解析当前登录用户：优先可信代理身份，回落到本地会话。"""
    # 1. Nginx + oauth2-proxy + Portal 注入的 Remote-*（生产）
    user = _remote_user(request, db)
    if user is not None:
        # 同步 session，供 CSRF / 审计 / 侧栏角标等复用
        if request.session.get("uid") != user.id:
            request.session["uid"] = user.id
        return user

    # 2. 本地会话登录（开发 / 直连）
    user_id: Optional[int] = request.session.get("uid")
    if not user_id:
        return None
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        request.session.clear()
        return None
    return user


def require_user(request: Request, user: Optional[User] = Depends(get_user)) -> User:
    if user is None:
        raise LoginRequired(str(request.url.path))
    return user


def require_roles(*roles: str) -> Callable:
    """生成角色依赖，未登录跳登录页，角色不符抛 403。"""

    def dependency(
        request: Request, user: Optional[User] = Depends(get_user)
    ) -> User:
        if user is None:
            raise LoginRequired(str(request.url.path))
        if user.role not in roles:
            raise PermissionDenied(
                f"当前账号（{user.role}）无权访问该页面"
            )
        return user

    return dependency


require_admin = require_roles(ROLE_ADMIN)
require_type1 = require_roles(ROLE_TYPE1, ROLE_ADMIN)
require_type2 = require_roles(ROLE_TYPE2, ROLE_ADMIN)
require_any = require_roles(*ROLE_ALL)


def audit(db: Session, user: Optional[User], action: str, detail: str = "") -> None:
    """写审计日志（失败不影响主流程）。"""
    try:
        from .models import AuditLog

        db.add(
            AuditLog(
                username=user.username if user else "-",
                action=action,
                detail=detail[:2000],
            )
        )
        db.commit()
    except Exception as exc:  # pragma: no cover
        logger.warning("审计日志写入失败: {}", exc)
