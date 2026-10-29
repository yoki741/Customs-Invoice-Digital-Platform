# -*- coding: utf-8 -*-
"""Jinja2 模板实例。"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from .auth import ensure_csrf
from .models import (
    ROLE_ADMIN,
    ROLE_LABELS,
    ROLE_TYPE1,
    ROLE_TYPE2,
    ROLE_TYPE3,
    CHECK_LABELS,
    CHECK_STYLES,
    CHECK_PENDING,
    CHECK_PASSED,
    CHECK_FAILED,
)

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

from .timefmt import fmt_local  # noqa: E402

templates.env.filters["localtime"] = lambda v, fmt="%Y-%m-%d %H:%M": fmt_local(v, fmt)
templates.env.globals.update(
    ROLE_ADMIN=ROLE_ADMIN,
    ROLE_TYPE1=ROLE_TYPE1,
    ROLE_TYPE2=ROLE_TYPE2,
    ROLE_TYPE3=ROLE_TYPE3,
    ROLE_LABELS=ROLE_LABELS,
    CHECK_LABELS=CHECK_LABELS,
    CHECK_STYLES=CHECK_STYLES,
    CHECK_PENDING=CHECK_PENDING,
    CHECK_PASSED=CHECK_PASSED,
    CHECK_FAILED=CHECK_FAILED,
    csrf_token=ensure_csrf,
)
