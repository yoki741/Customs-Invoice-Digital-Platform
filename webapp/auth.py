# -*- coding: utf-8 -*-
"""认证、权限依赖与日志。

支持两种认证来源：
1. Header 透传（生产，经 nginx）：X-Internal-Token 校验共享密钥 +
   X-Remote-User 提供用户名；首次访问自动建号（JIT），角色默认 type2
   或取 X-Remote-Role 提示。已有账号的角色以本系统「账号管理」为准。
2. 本地会话登录（开发 / 直连8000）：用户名密码 + session cookie。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Optional

import secrets

import bcrypt
from fastapi import Depends, Form, HTTPException, Request
from loguru import logger
from sqlalchemy.orm import Session

from core.config import (
    HEADER_AUTH_SECRET_PATH,
    LOG_DIR,
    SECRET_KEY_PATH,
    ensure_dirs,
)
from .models import ROLE_ADMIN, ROLE_TYPE1, ROLE_TYPE2, User, get_db

ROLE_ALL = (ROLE_ADMIN, ROLE_TYPE1, ROLE_TYPE2)

# Header 透传的用户名允许字符（防日志注入等）
_HEADER_USER_RE = re.compile(r"^[\w.\-@一-鿿]{1,64}$")


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


def get_header_auth_secret() -> str:
    """Header 认证共享密钥，首次运行自动生成；nginx 配置需与此一致。"""
    ensure_dirs()
    if HEADER_AUTH_SECRET_PATH.exists():
        return HEADER_AUTH_SECRET_PATH.read_text(encoding="utf-8").strip()
    key = secrets.token_urlsafe(32)
    HEADER_AUTH_SECRET_PATH.write_text(key, encoding="utf-8")
    try:
        HEADER_AUTH_SECRET_PATH.chmod(0o600)
    except OSError:
        pass
    return key


# ---- Header 透传认证（nginx） ----


def _header_auth_user(request: Request, db: Session) -> Optional[User]:
    """校验 nginx 注入的认证 header，JIT 开通账号。

    返回 None 表示本次请求不走 header 认证（无 token / token 错 /
    无用户名 / 用户名非法 / 用户被停用），回落到会话认证。
    """
    token = request.headers.get("X-Internal-Token", "")
    if not token:
        return None
    if not secrets.compare_digest(token, get_header_auth_secret()):
        logger.warning(
            "Header 认证 token 不匹配，来源 {}",
            request.client.host if request.client else "?",
        )
        return None

    username = request.headers.get("X-Remote-User", "").strip()
    if not username or not _HEADER_USER_RE.match(username):
        logger.warning("Header 认证用户名缺失或非法: {!r}", username)
        return None

    user = db.query(User).filter(User.username == username).one_or_none()
    if user is None:
        # JIT 开通：角色取 header 提示，不认识则默认 type2；
        # 已有账号一律以库内角色为准（管理员可在账号管理调整）。
        hint = request.headers.get("X-Remote-Role", "").strip().lower()
        role = hint if hint in ROLE_ALL else ROLE_TYPE2
        user = User(
            username=username,
            password_hash=hash_password(secrets.token_urlsafe(16)),
            role=role,
            display_name=username,
        )
        db.add(user)
        db.commit()
        audit(db, user, "jit.provision", f"Header 认证自动开通账号（角色 {role}）")
        logger.info("Header 认证 JIT 开通账号: {} ({})", username, role)
    if not user.is_active:
        logger.warning("Header 认证用户已被停用: {}", username)
        return None
    return user


# ---- 权限依赖 ----


def get_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    """解析当前登录用户：优先 Header 透传，回落到会话。"""
    # 1. nginx 透传（生产）
    user = _header_auth_user(request, db)
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
