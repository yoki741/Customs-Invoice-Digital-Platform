# -*- coding: utf-8 -*-
"""Header 透传认证（方案 B）测试：JIT 开通、token 校验、角色回落、停用拦截。"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

warnings.simplefilter("ignore")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from webapp.auth import get_header_auth_secret  # noqa: E402
from webapp.main import app  # noqa: E402

SECRET = get_header_auth_secret()


def _hdr(user: str, role: str = "", token: str = "") -> dict[str, str]:
    h = {"X-Internal-Token": token or SECRET, "X-Remote-User": user}
    if role:
        h["X-Remote-Role"] = role
    return h


def main() -> None:
    fails: list[str] = []

    def check(cond: bool, label: str) -> None:
        print(("[OK] " if cond else "[X] ") + label)
        if not cond:
            fails.append(label)

    client = TestClient(app, base_url="http://testserver", follow_redirects=False)

    # --- 1. 无 token：回落会话认证，访问受保护页跳登录 ---
    r = client.get("/invoices")
    check(r.status_code == 303 and "/login" in r.headers.get("location", ""),
          "无 header 时回落会话认证（跳登录）")

    # --- 2. token 错误：同样回落 ---
    r = client.get("/invoices", headers=_hdr("hacker", token="wrong-token"))
    check(r.status_code == 303 and "/login" in r.headers.get("location", ""),
          "token 错误拒绝 header 认证")

    # --- 3. 无用户名：回落 ---
    r = client.get("/invoices", headers={"X-Internal-Token": SECRET})
    check(r.status_code == 303, "缺少 X-Remote-User 回落会话认证")

    # --- 4. 用户名非法（含空格/控制符）：回落 ---
    r = client.get("/invoices", headers=_hdr("bad user;drop"))
    check(r.status_code == 303, "非法用户名拒绝")

    # --- 5. JIT 开通：新用户 + 默认角色 type2 ---
    r = client.get("/details", headers=_hdr("alice"))
    check(r.status_code == 200, "新用户 alice JIT 开通后可访问 /details（type2 权限）")
    r = client.get("/invoices", headers=_hdr("alice"))
    check(r.status_code == 403, "alice（type2）访问 /invoices 被拒")
    # 带 header 时 /login 应直接跳首页
    r = client.get("/login", headers=_hdr("alice"))
    check(r.status_code == 303 and r.headers.get("location") == "/",
          "header 认证用户访问 /login 直接跳首页")

    # --- 6. JIT 角色提示：X-Remote-Role=type1 ---
    r = client.get("/invoices", headers=_hdr("bob", role="type1"))
    check(r.status_code == 200, "bob JIT 开通为 type1，可访问 /invoices")
    r = client.get("/admin", headers=_hdr("bob", role="type1"))
    check(r.status_code == 403, "bob（type1）访问 /admin 被拒")

    # --- 7. 角色提示非法值 → 默认 type2 ---
    r = client.get("/details", headers=_hdr("carol", role="superuser"))
    check(r.status_code == 200, "carol 非法角色提示回落默认 type2")

    # --- 8. 已有账号角色以库内为准，忽略 header 提示 ---
    # alice 已是 type2，再带 role=admin 提示也不提权
    r = client.get("/admin", headers=_hdr("alice", role="admin"))
    check(r.status_code == 403, "已有账号忽略 X-Remote-Role 提权请求")

    # --- 9. 停用用户被拒（新 client，避免旧 session 回落干扰） ---
    from webapp.models import SessionLocal, User

    db = SessionLocal()
    u = db.query(User).filter(User.username == "carol").one()
    u.is_active = False
    db.commit()
    db.close()
    client9 = TestClient(app, base_url="http://testserver", follow_redirects=False)
    r = client9.get("/details", headers=_hdr("carol"))
    check(r.status_code == 303, "停用用户 header 认证被拒")

    # 恢复，避免影响其他测试
    db = SessionLocal()
    u = db.query(User).filter(User.username == "carol").one()
    u.is_active = True
    db.commit()
    db.close()

    # --- 10. 会话认证仍可用（不受 header 影响） ---
    client2 = TestClient(app, base_url="http://testserver", follow_redirects=False)
    r = client2.get("/login")
    import re

    m = re.search(r'name="csrf"\s+value="([^"]+)"', r.text)
    csrf = m.group(1) if m else ""
    r = client2.post("/login", data={
        "username": "admin", "password": "admin123", "csrf": csrf},
        follow_redirects=False)
    check(r.status_code == 303, "本地会话登录仍正常")
    r = client2.get("/admin")
    check(r.status_code == 200, "会话登录后可访问 /admin")

    # --- 11. 审计：JIT 开通有记录 ---
    from webapp.models import AuditLog

    db = SessionLocal()
    n = db.query(AuditLog).filter(AuditLog.action == "jit.provision",
                                  AuditLog.username.in_(["alice", "bob", "carol"])).count()
    db.close()
    check(n >= 3, f"JIT 开通已记审计日志（{n} 条）")

    print()
    if fails:
        print("FAILED:", len(fails))
        for f in fails:
            print("  -", f)
        raise SystemExit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
