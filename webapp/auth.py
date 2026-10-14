# -*- coding: utf-8 -*-
"""认证、权限依赖与日志。"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import secrets

import bcrypt
from fastapi import Depends, Form, HTTPException, Request
from loguru import logger
from sqlalchemy.orm import Session

from core.config import LOG_DIR, SECRET_KEY_PATH, ensure_dirs
from .models import ROLE_ADMIN, ROLE_TYPE1, ROLE_TYPE2, User, get_db

ROLE_ALL = (ROLE_ADMIN, ROLE_TYPE1, ROLE_TYPE2)


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


# ---- 权限依赖 ----


def get_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    """解析当前登录用户。"""
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
