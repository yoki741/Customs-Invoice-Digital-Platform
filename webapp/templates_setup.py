# -*- coding: utf-8 -*-
"""Jinja2 模板实例。"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from .auth import ensure_csrf
from .models import ROLE_ADMIN, ROLE_LABELS, ROLE_TYPE1, ROLE_TYPE2

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.globals.update(
    ROLE_ADMIN=ROLE_ADMIN,
    ROLE_TYPE1=ROLE_TYPE1,
    ROLE_TYPE2=ROLE_TYPE2,
    ROLE_LABELS=ROLE_LABELS,
    csrf_token=ensure_csrf,
)
