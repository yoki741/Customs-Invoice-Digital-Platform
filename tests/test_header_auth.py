# -*- coding: utf-8 -*-
"""可信代理身份（Remote-*）与子路径前缀测试（方案 L1 / B1 / C1）。

覆盖范围：
1. 无 / 非法 ``Remote-*`` 时回落本地会话认证；
2. JIT 开通、默认角色 type2、管理员组提示、停用拦截、组映射同步角色；
3. ``X-Forwarded-Prefix`` 下 Location 与页面链接都带 ``/customs-invoice``；
4. ``/healthz`` 免鉴权；
5. 非内网来源直接 403（防伪造 Remote-User）。

直接运行：``python tests/test_header_auth.py``
"""

from __future__ import annotations

import inspect
import os
import re
import sys
import uuid
import warnings
from pathlib import Path

warnings.simplefilter("ignore")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 本文件只覆盖「本地账号密码登录 + 可信代理身份」模式，
# 必须在导入 webapp 之前固定运行参数，避免容器 / 服务器上的
# CUSTOMS_AUTH_MODE=sso、CUSTOMS_SEED_ADMIN=0 影响断言。
# （SSO 模式的行为由 tests/test_sso_mode.py 在独立进程覆盖）
os.environ["CUSTOMS_AUTH_MODE"] = "local"
os.environ["CUSTOMS_SEED_ADMIN"] = "1"
os.environ.pop("APP_PREFIX", None)

from fastapi.testclient import TestClient  # noqa: E402

from webapp.main import app  # noqa: E402
from webapp.prefix import normalize_prefix  # noqa: E402

PREFIX = {"X-Forwarded-Prefix": "/customs-invoice"}
HTML = {"Accept": "text/html,application/xhtml+xml"}
# 每次运行使用独立用户名，保证 JIT 分支与审计断言可重复执行
RUN = uuid.uuid4().hex[:6]


def _hdr(user: str, groups: str = "") -> dict[str, str]:
    """构造 Portal 会注入的 Remote-* 请求头。

    Args:
        user (str): Remote-User 用户名。
        groups (str): Remote-Groups 逗号分隔的 Keycloak 组。

    Returns:
        dict[str, str]: 请求头。
    """
    headers = {
        "Remote-User": user,
        "Remote-Subject": f"sub-{user}",
        "Remote-Display-Name": user,
    }
    if groups:
        headers["Remote-Groups"] = groups
    return headers


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

    client = TestClient(app, base_url="http://testserver", follow_redirects=False)

    # --- 0. 前缀规范化 ---
    check(normalize_prefix("/customs-invoice/") == "/customs-invoice", "前缀去尾斜杠")
    check(normalize_prefix("") == "" and normalize_prefix("/") == "", "空前缀视为根路径部署")
    check(normalize_prefix("/customs invoice") == "", "含空格的前缀被拒绝")
    check(normalize_prefix("/a/b") == "", "多段前缀被拒绝")

    # --- 1. 健康检查免鉴权 ---
    r = client.get("/healthz")
    check(r.status_code == 200 and r.json().get("auth_mode") == "local",
          "/healthz 免鉴权且报告 local 模式")

    # --- 2. 无 Remote-*：回落本地会话认证 ---
    r = client.get("/invoices", headers=HTML)
    check(r.status_code == 303 and "/login" in r.headers.get("location", ""),
          "无身份头时回落会话认证（跳登录）")

    # --- 3. 只有 Subject 没有 User：回落 ---
    r = client.get("/invoices", headers={"Remote-Subject": "sub-only"})
    check(r.status_code == 303, "缺少 Remote-User 回落会话认证")

    # --- 4. 用户名非法（空格/分号注入）：回落 ---
    r = client.get("/invoices", headers=_hdr("bad user;drop"))
    check(r.status_code == 303, "非法 Remote-User 拒绝")

    # --- 5. JIT 开通：默认 type2 ---
    u_type2 = f"alice_{RUN}"
    r = client.get("/details", headers=_hdr(u_type2))
    check(r.status_code == 200, "新用户 JIT 开通后可访问 /details（默认 type2）")
    r = client.get("/invoices", headers=_hdr(u_type2))
    check(r.status_code == 403, "type2 访问 /invoices 被拒")
    r = client.get("/login", headers=_hdr(u_type2))
    check(r.status_code == 303 and r.headers.get("location") == "/",
          "已认证用户访问 /login 直接跳首页")

    # --- 6. 本平台管理组 → admin；Portal 全局 /admin 不再授予本平台管理员 ---
    u_admin = f"dave_{RUN}"
    r = client.get(
        "/admin",
        headers=_hdr(u_admin, groups="/apps/customs-invoice,/customs-invoice-admin"),
    )
    check(r.status_code == 200, "带 /customs-invoice-admin 组的 JIT 账号获得管理员权限")
    u_portal_super = f"erin_{RUN}"
    r = client.get(
        "/admin", headers=_hdr(u_portal_super, groups="/apps/customs-invoice,/admin")
    )
    check(r.status_code == 403, "仅有 Portal 全局 /admin 组不授予本平台管理员权限")

    # --- 7. 组映射是角色权威源：已有账号同样跟随组变更 ---
    u_plain = f"bob_{RUN}"
    r = client.get("/details", headers=_hdr(u_plain, groups="/apps/customs-invoice"))
    check(r.status_code == 200, "普通组 JIT 账号为 type2")
    r = client.get(
        "/admin",
        headers=_hdr(u_plain, groups="/apps/customs-invoice,/customs-invoice-admin"),
    )
    check(r.status_code == 200, "已有账号跟随组变更提升为管理员（组映射为准）")
    r = client.get("/admin", headers=_hdr(u_plain, groups="/apps/customs-invoice"))
    check(r.status_code == 403, "移出管理员组后立即降级（组映射为准）")

    # --- 8. 子路径前缀：Location 与页面链接 ---
    anon = TestClient(app, base_url="http://testserver", follow_redirects=False)
    r = anon.get("/invoices", headers={**PREFIX, **HTML})
    location = r.headers.get("location", "")
    check(r.status_code == 303 and location.startswith("/customs-invoice/login"),
          f"未登录跳转带子路径前缀（{location}）")
    r = anon.get("/details", headers={**PREFIX, **_hdr(u_type2)})
    check(r.status_code == 200 and "/customs-invoice/static/css/app.css" in r.text,
          "静态资源链接带子路径前缀")
    check('href="/details"' not in r.text, "页面中没有根路径绝对链接")
    check('href="/customs-invoice/details"' in r.text, "导航链接带子路径前缀")
    check('window.APP_PREFIX = "/customs-invoice"' in r.text, "页面暴露 APP_PREFIX 供脚本使用")
    r = anon.get("/customs-invoice/details", headers=_hdr(u_type2))
    check(r.status_code == 404, "应用不处理带前缀的路径（前缀由 Nginx 剥离）")

    # --- 9. 非内网来源一律 403（防伪造 Remote-User） ---
    if "client" in inspect.signature(TestClient.__init__).parameters:
        evil = TestClient(
            app, base_url="http://testserver", follow_redirects=False,
            client=("8.8.8.8", 12345),
        )
        r = evil.get("/healthz", headers=_hdr(u_admin, groups="/customs-invoice-admin"))
        check(r.status_code == 403, "非内网来源伪造 Remote-User 被 403 拒绝")
    else:
        print("[SKIP] 当前 Starlette 的 TestClient 不支持自定义来源地址")

    # --- 10. 停用用户被拒 ---
    from webapp.models import ROLE_ADMIN, SessionLocal, User

    u_off = f"carol_{RUN}"
    client.get("/details", headers=_hdr(u_off))
    db = SessionLocal()
    row = db.query(User).filter(User.username == u_off).one()
    row.is_active = False
    db.commit()
    db.close()
    off = TestClient(app, base_url="http://testserver", follow_redirects=False)
    r = off.get("/details", headers=_hdr(u_off))
    check(r.status_code == 303, "停用用户即使带合法身份头也被拒")

    # --- 11. 本地会话登录仍可用（开发模式） ---
    # 用例与库的创建顺序无关：若库里没有 admin（例如库由 SSO 模式测试先创建），
    # 这里按首次启动的同等条件补建一个本地管理员。
    from webapp.auth import hash_password

    db = SessionLocal()
    if db.query(User).filter(User.username == "admin").one_or_none() is None:
        db.add(
            User(
                username="admin",
                password_hash=hash_password("admin123"),
                role=ROLE_ADMIN,
                display_name="系统管理员",
                dept="关务",
            )
        )
        db.commit()
    db.close()
    client2 = TestClient(app, base_url="http://testserver", follow_redirects=False)
    r = client2.get("/login")
    match = re.search(r'name="csrf"\s+value="([^"]+)"', r.text)
    csrf = match.group(1) if match else ""
    r = client2.post("/login", data={"username": "admin", "password": "admin123",
                                     "csrf": csrf})
    check(r.status_code == 303, "本地会话登录仍正常（admin/admin123）")
    r = client2.get("/admin")
    check(r.status_code == 200, "会话登录后可访问 /admin")

    # --- 12. JIT 开通写审计日志 ---
    from webapp.models import AuditLog

    db = SessionLocal()
    count = (
        db.query(AuditLog)
        .filter(AuditLog.action == "jit.provision",
                AuditLog.username.in_([u_type2, u_admin, u_plain, u_off]))
        .count()
    )
    db.close()
    check(count >= 4, f"JIT 开通已记审计日志（{count} 条）")

    print()
    if fails:
        print("FAILED:", len(fails))
        for item in fails:
            print("  -", item)
        raise SystemExit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
