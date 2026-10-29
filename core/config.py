# -*- coding: utf-8 -*-
"""平台路径与基础配置。

所有目录都相对仓库根目录定位，保证 EXE / 开发环境 / Web 环境行为一致。
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# 资源文件（与 invoice_gui.py 保持同一套）
TEMPLATE_DIR = BASE_DIR / "模板文件"
PRICE_FILE = BASE_DIR / "Eastex Vanco CDC 布料单价.xlsx"

# Web 运行时目录
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
HISTORY_DIR = DATA_DIR / "file_history"
LOG_DIR = BASE_DIR / "logs"

DB_PATH = DATA_DIR / "app.db"
SECRET_KEY_PATH = DATA_DIR / "secret_key"
# Header 认证共享密钥（nginx proxy_set_header X-Internal-Token 与此一致）
HEADER_AUTH_SECRET_PATH = DATA_DIR / "header_auth_secret"

TRANSPORT_TEMPLATES = {
    "海运": "海运模板.xlsx",
    "空运": "空运模板.xlsx",
    "陆运": "陆运模板.xlsx",
}

# 装柜列表（Shipment List）模板与输出目录
SHIPMENT_LIST_TEMPLATE = BASE_DIR / "Shipment List 列表模板简化 补充.xlsx"
SHIPMENT_LIST_DIR = OUTPUT_DIR / "装柜列表"

# 导出后是否用 Excel 开启“修订跟踪”（本地修改可回溯）。
# 该功能会在 Excel 标题栏显示「[共享]」，容易被同事察觉；默认关闭，仅在有需要时
# 设 INVOICE_TRACK_CHANGES=1 手动开启。
ENABLE_TRACK_CHANGES = os.environ.get("INVOICE_TRACK_CHANGES", "0") != "0"

# 文件内嵌“修改记录器”：极隐藏 _LOG 表 + 工作簿结构保护密码。
# 密码用于防止同事在 Excel 界面取消隐藏 _LOG 表（Excel 保护密码强度有限，防君子）。
RECORDER_PASSWORD = os.environ.get("INVOICE_RECORDER_PASSWORD", "Invoice@2026")
# 是否在导出时注入修改记录器 VBA。该方案已停用：依赖本机 Excel + 破解源文件
# VBA 工程锁，成本高且不稳定；取证改走「指纹 + 内容比对」。默认关闭，除非
# 显式设 INVOICE_RECORDER=1。
ENABLE_RECORDER = os.environ.get("INVOICE_RECORDER", "0") != "0"


def ensure_dirs() -> None:
    """创建运行所需目录。"""
    for path in (DATA_DIR, UPLOAD_DIR, OUTPUT_DIR, SHIPMENT_LIST_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)
