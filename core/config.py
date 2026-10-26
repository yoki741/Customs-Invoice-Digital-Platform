# -*- coding: utf-8 -*-
"""平台路径与基础配置。

所有目录都相对仓库根目录定位，保证 EXE / 开发环境 / Web 环境行为一致。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

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
SHIPMENT_LIST_TEMPLATE = TEMPLATE_DIR / "装柜列表模板.xlsx"
SHIPMENT_LIST_DIR = OUTPUT_DIR / "装柜列表"

# 导出后是否用 Excel 开启“修订跟踪”（本地修改可回溯）。
# Excel 不可用时自动跳过；设 INVOICE_TRACK_CHANGES=0 可关闭。
ENABLE_TRACK_CHANGES = os.environ.get("INVOICE_TRACK_CHANGES", "1") != "0"


# ---- 认证模式（与 DTD_nginx Portal 对接）----
# sso  ：生产。身份由 Nginx + oauth2-proxy + Portal Authorizer 注入 Remote-* header，
#        本平台不提供账号密码登录，登出/改密交回 Portal。
# local：开发 / 直连 8000。沿用本地账号密码 + session。
AUTH_MODE = os.environ.get("CUSTOMS_AUTH_MODE", "local").strip().lower()
if AUTH_MODE not in ("sso", "local"):
    AUTH_MODE = "local"
SSO_MODE = AUTH_MODE == "sso"

# 未认证时的登录入口。SSO 模式交给 oauth2-proxy 发起 Keycloak 登录。
SSO_LOGIN_URL = os.environ.get("CUSTOMS_SSO_LOGIN_URL", "/oauth2/start").strip() or "/oauth2/start"

# Portal 容器内部地址，用于服务端拼接完整的登出链（oauth2-proxy → Keycloak → 首页）。
PORTAL_INTERNAL_URL = os.environ.get(
    "CUSTOMS_PORTAL_INTERNAL_URL", "http://DKT_kc_portal:8001"
).strip().rstrip("/")
PORTAL_LOGOUT_API = f"{PORTAL_INTERNAL_URL}/api/auth/logout/"
# Portal 不可用时的兜底登出地址
OAUTH2_SIGNOUT_URL = os.environ.get(
    "CUSTOMS_OAUTH2_SIGNOUT_URL", "/oauth2/sign_out"
).strip() or "/oauth2/sign_out"

# ---- 统一登录组 → 角色映射（Keycloak 为权限的唯一权威源）----
# 格式："/组路径:角色" 逗号分隔，角色取 admin / type1 / type2。
# 命中多个组时按 ROLE_PRIORITY 取最高权限（admin > type1 > type2）。
# 统一登录模式下每次请求都会把命中结果同步进 users.role，
# 因此在 Keycloak 里调整组成员后刷新页面即生效，无需进本平台管理后台。
ROLE_PRIORITY = ("admin", "type1", "type2")

DEFAULT_ROLE_GROUPS = (
    "/customs-invoice-admin:admin,"
    "/customs-invoice-type1:type1,"
    "/apps/customs-invoice:type2"  # 基础访问权 = 明细查看
)
# 注意：Portal 全局管理员组 /admin **不再**映射为本平台 admin。
# 本平台管理员只由 /customs-invoice-admin 授予（现网仅指定人员），
# 避免 Portal 超管自动获得报关平台的账号管理/导出等权限。
# 若某次确实要让 /admin 组成员当本平台管理员，加环境变量
#   CUSTOMS_ROLE_GROUPS="/admin:admin,<上面的默认值>"
# 即可，无需改代码。


def _parse_role_groups(raw: str) -> dict[str, str]:
    """把 ``/组:角色`` 配置解析成 组路径 -> 角色 的字典。

    Args:
        raw (str): 逗号分隔的映射配置。

    Returns:
        dict[str, str]: 组路径 -> 角色；非法项忽略，角色不在
        ``ROLE_PRIORITY`` 里的项按 type2 处理（最小权限兜底）。
    """
    mapping: dict[str, str] = {}
    for chunk in raw.split(","):
        item = chunk.strip()
        if not item or ":" not in item:
            continue
        path, _, role = item.rpartition(":")
        path = path.strip()
        role = role.strip().lower()
        if not path.startswith("/"):
            continue
        mapping[path] = role if role in ROLE_PRIORITY else "type2"
    return mapping


_ROLE_GROUP_MAP = _parse_role_groups(
    os.environ.get("CUSTOMS_ROLE_GROUPS", DEFAULT_ROLE_GROUPS)
)
# 兼容旧配置：CUSTOMS_ADMIN_GROUPS 里的组一律视为管理员
# 默认只认本平台的专用管理组，不含 Portal 全局 /admin
for _group in os.environ.get(
    "CUSTOMS_ADMIN_GROUPS", "/customs-invoice-admin"
).split(","):
    if _group.strip():
        _ROLE_GROUP_MAP.setdefault(_group.strip(), "admin")

# (组路径, 角色)，按权限从高到低排列，便于取最高权限
ROLE_GROUP_MAP: tuple[tuple[str, str], ...] = tuple(
    (path, role)
    for role in ROLE_PRIORITY
    for path, mapped in _ROLE_GROUP_MAP.items()
    if mapped == role
)


def group_match_keys(raw: str) -> set[str]:
    """把组路径展开成可用于比对的候选键。

    Keycloak 的 ``groups`` claim 由 ``group-membership`` mapper 生成，现网配置为
    ``full.path=false``：``/admin`` 变成 ``admin``，嵌套组 ``/apps/customs-invoice``
    只给叶子名 ``customs-invoice``。因此比对时必须同时接受
    「带斜杠全路径」「无斜杠全路径」「叶子名」三种写法。

    Args:
        raw (str): 组路径或组名，可带前导斜杠。

    Returns:
        set[str]: 候选键集合；空输入返回空集合。
    """
    value = raw.strip()
    if not value:
        return set()
    if not value.startswith("/"):
        value = "/" + value
    value = value.rstrip("/") or "/"
    keys = {value}
    leaf = value.rsplit("/", 1)[-1].strip()
    if leaf:
        keys.add(leaf)
    return keys


# 预展开的匹配表：(候选键, 角色, 原始组路径)，顺序同 ROLE_GROUP_MAP
_ROLE_MATCH_TABLE: tuple[tuple[frozenset[str], str, str], ...] = tuple(
    (frozenset(group_match_keys(path)), role, path)
    for path, role in ROLE_GROUP_MAP
)


def resolve_role_from_groups(groups: Iterable[str]) -> tuple[str, str]:
    """按组映射解析角色，多个组命中时取最高权限。

    Args:
        groups (Iterable[str]): ``Remote-Groups`` 分隔后的组值，可带或不带前导斜杠。

    Returns:
        tuple[str, str]: ``(角色, 命中的配置组路径)``；未命中返回 ``("", "")``，
        表示本次身份不携带角色决定，调用方应沿用库内角色。
    """
    incoming: set[str] = set()
    for item in groups:
        incoming |= group_match_keys(item)
    if not incoming:
        return "", ""
    for keys, role, path in _ROLE_MATCH_TABLE:
        if keys & incoming:
            return role, path
    return "", ""


# 是否在每次请求时把组映射结果同步进 users.role（1 开 / 0 关，默认开）
ROLE_SYNC = os.environ.get("CUSTOMS_ROLE_SYNC", "1") != "0"

# JIT 开通新账号且未命中任何组映射时的默认角色
_DEFAULT_ROLE = os.environ.get("CUSTOMS_DEFAULT_ROLE", "type2").strip().lower()
DEFAULT_ROLE = _DEFAULT_ROLE if _DEFAULT_ROLE in ROLE_PRIORITY else "type2"

# 兼容旧代码路径：管理员组集合（由映射推导，不再单独配置）
ADMIN_GROUPS = frozenset(
    path for path, role in ROLE_GROUP_MAP if role == "admin"
)

# 空库时是否创建默认管理员 admin/admin123（仅本地开发；生产必须为 0）。
SEED_ADMIN = os.environ.get("CUSTOMS_SEED_ADMIN", "1") != "0"


def ensure_dirs() -> None:
    """创建运行所需目录。"""
    for path in (DATA_DIR, UPLOAD_DIR, OUTPUT_DIR, SHIPMENT_LIST_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)
