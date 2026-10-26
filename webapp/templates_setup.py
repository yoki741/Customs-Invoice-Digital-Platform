# -*- coding: utf-8 -*-
"""Jinja2 模板实例。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import Request
from fastapi.templating import Jinja2Templates

from core.config import SSO_MODE
from .auth import ensure_csrf
from .models import (
    ROLE_ADMIN,
    ROLE_LABELS,
    ROLE_TYPE1,
    ROLE_TYPE2,
    CHECK_LABELS,
    CHECK_STYLES,
    CHECK_PENDING,
    CHECK_PASSED,
    CHECK_FAILED,
)
from .prefix import configured_prefix, current_prefix

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def ui_context(request: Optional[Request] = None) -> dict[str, Any]:
    """向所有模板注入渲染公共变量。

    Args:
        request (Optional[Request]): 当前请求；Starlette 会传入。

    Returns:
        dict[str, Any]: ``prefix`` 为当前子路径前缀（根路径部署时为空串），
        ``sso_mode`` 表示是否由 Portal 统一登录接管认证。
    """
    prefix = current_prefix(request.scope) if request is not None else ""
    return {
        "prefix": prefix or configured_prefix(),
        "sso_mode": SSO_MODE,
    }


templates = Jinja2Templates(directory=str(TEMPLATE_DIR), context_processors=[ui_context])
templates.env.globals.update(
    ROLE_ADMIN=ROLE_ADMIN,
    ROLE_TYPE1=ROLE_TYPE1,
    ROLE_TYPE2=ROLE_TYPE2,
    ROLE_LABELS=ROLE_LABELS,
    CHECK_LABELS=CHECK_LABELS,
    CHECK_STYLES=CHECK_STYLES,
    CHECK_PENDING=CHECK_PENDING,
    CHECK_PASSED=CHECK_PASSED,
    CHECK_FAILED=CHECK_FAILED,
    csrf_token=ensure_csrf,
)

