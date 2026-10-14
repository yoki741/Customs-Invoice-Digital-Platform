# -*- coding: utf-8 -*-
"""平台路径与基础配置。

所有目录都相对仓库根目录定位，保证 EXE / 开发环境 / Web 环境行为一致。
"""

from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# 资源文件（与 invoice_gui.py 保持同一套）
TEMPLATE_DIR = BASE_DIR / "模板文件"
PRICE_FILE = BASE_DIR / "Eastex Vanco CDC 布料单价.xlsx"

# Web 运行时目录
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
LOG_DIR = BASE_DIR / "logs"

DB_PATH = DATA_DIR / "app.db"
SECRET_KEY_PATH = DATA_DIR / "secret_key"

TRANSPORT_TEMPLATES = {
    "海运": "海运模板.xlsx",
    "空运": "空运模板.xlsx",
    "陆运": "陆运模板.xlsx",
}


def ensure_dirs() -> None:
    """创建运行所需目录。"""
    for path in (DATA_DIR, UPLOAD_DIR, OUTPUT_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)
