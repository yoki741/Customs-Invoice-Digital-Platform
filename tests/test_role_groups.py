# -*- coding: utf-8 -*-
"""统一登录组 → 角色映射测试。

覆盖「Keycloak 组决定本平台角色」的全部路径：JIT 建号角色、组变更同步、
未命中组不降级、停用优先、管理页角色只读。

必须在 ``import webapp.main`` 之前把 ``CUSTOMS_AUTH_MODE`` 设为 ``sso``，
因此本文件设计为独立进程运行：

    python tests/test_role_groups.py

（pytest 同进程下会因 config 已被 local 模式导入而自动跳过。）
"""

from __future__ import annotations

import os
import re
import sys
import uuid
import warnings
from pathlib import Path

warnings.simplefilter("ignore")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["CUSTOMS_AUTH_MODE"] = "sso"
os.environ["CUSTOMS_SEED_ADMIN"] = "0"
os.environ.pop("CUSTOMS_ROLE_GROUPS", None)

from fastapi.testclient import TestClient  # noqa: E402

from core.config import (  # noqa: E402
    ROLE_GROUP_MAP,
    SSO_MODE,
    group_match_keys,
    resolve_role_from_groups,
)
from webapp.main import app  # noqa: E402
from webapp.models import (  # noqa: E402
    ROLE_ADMIN,
    ROLE_TYPE1,
    ROLE_TYPE2,
    SessionLocal,
    User,
)

if not SSO_MODE:
    try:
        import pytest

        pytest.skip(
            "需独立进程运行：python tests/test_role_groups.py",
            allow_module_level=True,
        )
    except ImportError:
        raise SystemExit("请单独运行本文件：python tests/test_role_groups.py")

HTML = {"Accept": "text/html,application/xhtml+xml"}
RUN = uuid.uuid4().hex[:6]

GROUP_ADMIN = "/admin"  # Portal 全局管理员组：不再授予本平台 admin（仅保证能进门）
GROUP_ROLE_ADMIN = "/customs-invoice-admin"
GROUP_TYPE1 = "/customs-invoice-type1"
GROUP_BASE = "/apps/customs-invoice"


def _hdr(user: str, *groups: str) -> dict[str, str]:
    """构造 Portal 注入的 Remote-* 身份头。

    Args:
        user (str): 用户名。
        *groups (str): Keycloak 组路径。

    Returns:
        dict[str, str]: 请求头。
    """
    return {
        "Remote-User": user,
        "Remote-Subject": f"sub-{user}",
        "Remote-Display-Name": user,
        "Remote-Groups": ",".join(groups),
    }


def _query(username: str) -> User | None:
    """按账号查询用户行（脱离会话返回快照）。

    Args:
        username (str): 账号。

    Returns:
        User | None: 用户对象；不存在时为 None。
    """
    with SessionLocal() as db:
        return db.query(User).filter(User.username == username).one_or_none()


def _role_of(username: str) -> str | None:
    """读取库内当前角色。

    Args:
        username (str): 账号。

    Returns:
        str | None: 角色值；账号不存在时为 None。
    """
    row = _query(username)
    return row.role if row else None


def _user_id(username: str) -> int:
    """查询账号主键。

    Args:
        username (str): 账号。

    Returns:
        int: 账号 ID；不存在时为 0。
    """
    row = _query(username)
    return row.id if row else 0


def _set_active(username: str, active: bool) -> None:
    """直接改库内的启用状态（模拟管理员在账号管理页停用）。

    Args:
        username (str): 账号。
        active (bool): 目标状态。
    """
    with SessionLocal() as db:
        row = db.query(User).filter(User.username == username).one_or_none()
        if row is not None:
            row.is_active = active
            db.commit()


def _csrf(client: TestClient, username: str) -> str:
    """从账号管理页抓取 CSRF token。

    Args:
        client (TestClient): 测试客户端。
        username (str): 拥有管理员权限的账号。

    Returns:
        str: CSRF token；抓不到时为空串。
    """
    page = client.get("/admin/users", headers=_hdr(username, GROUP_ROLE_ADMIN))
    match = re.search(r'name="csrf" value="([^"]+)"', page.text)
    return match.group(1) if match else ""

def main() -> None:
    """逐项校验并汇总失败。"""
    fails: list[str] = []

    def check(cond: bool, label: str) -> None:
        """记录一条断言结果。

        Args:
            cond (bool): 断言是否成立。
            label (str): 用例描述。
        """
        print(("[OK] " if cond else "[X] ") + label)
        if not cond:
            fails.append(label)

    # SSO 模式下 SessionMiddleware 设置 https_only=True，Cookie 带 Secure 标记，
    # 因此必须用 https 基址，否则会话（含 CSRF token）无法在请求间保留。
    client = TestClient(app, base_url="https://testserver", follow_redirects=False)
    check(SSO_MODE is True, "已按 SSO 模式加载配置")
    mapping = dict(ROLE_GROUP_MAP)
    check(
        mapping.get(GROUP_ROLE_ADMIN) == ROLE_ADMIN
        and mapping.get(GROUP_TYPE1) == ROLE_TYPE1
        and mapping.get(GROUP_BASE) == ROLE_TYPE2,
        f"默认组映射生效（{ROLE_GROUP_MAP}）",
    )
    check(
        GROUP_ADMIN not in mapping,
        f"Portal 全局 {GROUP_ADMIN} 组不再映射为本平台管理员（{ROLE_GROUP_MAP}）",
    )

    # --- 1. Portal 全局管理员组 /admin → 只有进门资格，不再自动升为 admin ---
    portal_super = f"role_adminsuper_{RUN}"
    r = client.get("/details", headers=_hdr(portal_super, GROUP_BASE, GROUP_ADMIN))
    check(r.status_code == 200, f"/admin 组用户可进业务页（{r.status_code}）")
    check(_role_of(portal_super) == ROLE_TYPE2, "仅有 /admin 组时角色按默认 type2，不再自动 admin")
    r = client.get("/admin", headers=_hdr(portal_super, GROUP_BASE, GROUP_ADMIN))
    check(r.status_code == 403, "/admin 组用户访问本平台 /admin 被拒（403）")

    # --- 2. 角色专用管理组 /customs-invoice-admin → admin ---
    app_admin = f"role_appadmin_{RUN}"
    r = client.get("/admin/users", headers=_hdr(app_admin, GROUP_ROLE_ADMIN))
    check(r.status_code == 200, "角色管理组用户可进账号管理页")
    check(_role_of(app_admin) == ROLE_ADMIN, "角色管理组 JIT 建号角色为 admin")
    portal_admin = app_admin

    # --- 3. 第一类组 → type1：业务页可进、管理后台拒绝 ---
    type1_user = f"role_type1_{RUN}"
    r = client.get("/invoices", headers=_hdr(type1_user, GROUP_BASE, GROUP_TYPE1))
    check(r.status_code == 200, "type1 组用户可进发票列表")
    check(_role_of(type1_user) == ROLE_TYPE1, "type1 组 JIT 建号角色为 type1")
    r = client.get("/admin", headers=_hdr(type1_user, GROUP_BASE, GROUP_TYPE1))
    check(r.status_code == 403, "type1 组用户访问 /admin 被拒（403）")

    # --- 4. 只有基础访问组 → type2：明细可看、管理后台拒绝 ---
    type2_user = f"role_type2_{RUN}"
    r = client.get("/details", headers=_hdr(type2_user, GROUP_BASE))
    check(r.status_code == 200, "基础组用户可进明细页")
    check(_role_of(type2_user) == ROLE_TYPE2, "基础组 JIT 建号角色为 type2")
    r = client.get("/admin", headers=_hdr(type2_user, GROUP_BASE))
    check(r.status_code == 403, "基础组用户访问 /admin 被拒（403）")

    # --- 5. 组变更即时同步：加组升级 / 移组降级（无需进本平台后台） ---
    sync_user = f"role_sync_{RUN}"
    client.get("/details", headers=_hdr(sync_user, GROUP_BASE))
    check(_role_of(sync_user) == ROLE_TYPE2, "同步用例初始化为 type2")
    r = client.get("/admin", headers=_hdr(sync_user, GROUP_BASE, GROUP_ROLE_ADMIN))
    check(
        r.status_code == 200 and _role_of(sync_user) == ROLE_ADMIN,
        "加入 /customs-invoice-admin 组后刷新即提升为 admin",
    )
    client.get("/invoices", headers=_hdr(sync_user, GROUP_BASE, GROUP_TYPE1))
    check(_role_of(sync_user) == ROLE_TYPE1, "改组为 type1 组后角色降为 type1")
    r = client.get("/admin", headers=_hdr(sync_user, GROUP_BASE, GROUP_TYPE1))
    check(r.status_code == 403, "降级后立即失去管理后台权限")
    client.get("/details", headers=_hdr(sync_user, GROUP_BASE))
    check(_role_of(sync_user) == ROLE_TYPE2, "移出高权限组后回落 type2")

    # --- 6. 未命中任何组 → 不改库内角色（不因组名写错而误降级） ---
    client.get("/admin", headers=_hdr(sync_user, GROUP_ROLE_ADMIN))
    r = client.get("/admin", headers=_hdr(sync_user))
    check(
        r.status_code == 200 and _role_of(sync_user) == ROLE_ADMIN,
        "无组信息时保留库内 admin，不被降级",
    )
    client.get("/details", headers=_hdr(sync_user, "/apps/some-other-app"))
    check(_role_of(sync_user) == ROLE_ADMIN, "无关组不影响库内角色")

    # --- 7. 停用优先：即使组给 admin 也进不来 ---
    _set_active(sync_user, False)
    r = client.get("/admin", headers={**HTML, **_hdr(sync_user, GROUP_ROLE_ADMIN)})
    location = r.headers.get("location", "")
    check(
        r.status_code == 303 and location.startswith("/oauth2/start"),
        f"已停用账号即使有管理组也不放行（{r.status_code} {location}）",
    )
    check(_role_of(sync_user) == ROLE_ADMIN, "停用用户角色不被改写")
    _set_active(sync_user, True)

    # --- 8. 账号管理页在 SSO 模式下角色只读 ---
    r = client.get("/admin/users", headers=_hdr(portal_admin, GROUP_ROLE_ADMIN))
    check(
        "由 Keycloak 组决定" in r.text and "统一登录模式" in r.text,
        "账号管理页显示角色只读说明",
    )
    check(
        f'/admin/users/{_user_id(sync_user)}/role"' not in r.text,
        "账号管理页不再渲染改角色表单",
    )
    r = client.post(
        f"/admin/users/{_user_id(sync_user)}/role",
        data={"role": ROLE_TYPE2, "csrf": _csrf(client, portal_admin)},
        headers=_hdr(portal_admin, GROUP_ROLE_ADMIN),
    )
    check(
        r.status_code == 303 and _role_of(sync_user) == ROLE_ADMIN,
        "SSO 模式下改角色接口被拒绝（角色仍为 admin）",
    )

    # --- 9. 现网 Keycloak 实际下发格式（full.path=false：无前导斜杠、嵌套组只给叶子名） ---
    leaf_admin = f"role_leafadmin_{RUN}"
    client.get("/details", headers=_hdr(leaf_admin, "customs-invoice", "admin"))
    r = client.get("/admin", headers=_hdr(leaf_admin, "customs-invoice", "admin"))
    check(
        r.status_code == 403 and _role_of(leaf_admin) == ROLE_TYPE2,
        "无斜杠组 admin（full.path=false）不授予本平台 admin",
    )
    check(
        resolve_role_from_groups(["admin"]) == ("", ""),
        "Portal 全局 admin 组不解析出本平台角色",
    )
    leaf_type1 = f"role_leaftype1_{RUN}"
    r = client.get("/invoices", headers=_hdr(leaf_type1, "customs-invoice", "customs-invoice-type1"))
    check(
        r.status_code == 200 and _role_of(leaf_type1) == ROLE_TYPE1,
        "无斜杠组 customs-invoice-type1 解析为 type1",
    )
    leaf_type2 = f"role_leaftype2_{RUN}"
    r = client.get("/details", headers=_hdr(leaf_type2, "customs-invoice"))
    check(
        r.status_code == 200 and _role_of(leaf_type2) == ROLE_TYPE2,
        "嵌套组叶子名 customs-invoice 解析为 type2",
    )
    nested_admin = f"role_nestedadmin_{RUN}"
    client.get("/details", headers=_hdr(nested_admin, "customs-invoice"))
    r = client.get("/admin", headers=_hdr(nested_admin, "customs-invoice", "customs-invoice-admin"))
    check(
        r.status_code == 200 and _role_of(nested_admin) == ROLE_ADMIN,
        "无斜杠组 customs-invoice-admin 解析为 admin",
    )
    check(
        group_match_keys("apps/customs-invoice")
        == {"/apps/customs-invoice", "customs-invoice"},
        "group_match_keys 同时生成全路径与叶子名候选键",
    )
    check(
        resolve_role_from_groups(["/customs-invoice-admin"]) == (ROLE_ADMIN, "/customs-invoice-admin")
        and resolve_role_from_groups(["customs-invoice-admin"]) == (ROLE_ADMIN, "/customs-invoice-admin"),
        "带斜杠/不带斜杠两种写法解析结果一致",
    )
    check(
        resolve_role_from_groups(["/apps/some-other-app"]) == ("", ""),
        "无关组不解析出角色",
    )

    print()
    if fails:
        print("FAILED:", len(fails))
        for item in fails:
            print("  -", item)
        raise SystemExit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
