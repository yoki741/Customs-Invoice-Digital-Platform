# -*- coding: utf-8 -*-
"""统一登录（SSO）模式测试：401 / oauth2 跳转 / 登出链 / 改密提示 / 子路径前缀。

必须在 ``import webapp.main`` 之前把 ``CUSTOMS_AUTH_MODE`` 设为 ``sso``，
因此本文件设计为独立进程运行：

    python tests/test_sso_mode.py

（pytest 同进程下会因 config 已被 local 模式导入而自动跳过。）
"""

from __future__ import annotations

import json
import os
import sys
import uuid
import warnings
from pathlib import Path

warnings.simplefilter("ignore")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["CUSTOMS_AUTH_MODE"] = "sso"
os.environ["CUSTOMS_SEED_ADMIN"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from core.config import SSO_MODE  # noqa: E402
from webapp.main import app  # noqa: E402

if not SSO_MODE:
    try:
        import pytest

        pytest.skip("需独立进程运行：python tests/test_sso_mode.py", allow_module_level=True)
    except ImportError:
        raise SystemExit("请单独运行本文件：python tests/test_sso_mode.py")

HTML = {"Accept": "text/html,application/xhtml+xml"}
JSON_ACCEPT = {"Accept": "application/json"}
PREFIX = {"X-Forwarded-Prefix": "/customs-invoice"}
RUN = uuid.uuid4().hex[:6]
PORTAL_REDIRECT = "https://portal.example.com/oauth2/sign_out?rd=keycloak-logout"


class _FakeResponse:
    """模拟 Portal 登出接口的响应对象。"""

    def __init__(self, payload: dict) -> None:
        """保存返回体。

        Args:
            payload (dict): 接口返回的 JSON。
        """
        self._body = json.dumps(payload).encode("utf-8")

    def read(self, _size: int = -1) -> bytes:
        """返回响应体。

        Args:
            _size (int): 忽略的读取长度。

        Returns:
            bytes: JSON 字节串。
        """
        return self._body

    def __enter__(self) -> "_FakeResponse":
        """进入上下文。

        Returns:
            _FakeResponse: 自身。
        """
        return self

    def __exit__(self, *_exc: object) -> bool:
        """退出上下文。

        Args:
            _exc (object): 忽略的异常信息。

        Returns:
            bool: 不吞异常。
        """
        return False


def _hdr(user: str) -> dict[str, str]:
    """构造 Portal 注入的 Remote-* 身份头。

    Args:
        user (str): 用户名。

    Returns:
        dict[str, str]: 请求头。
    """
    return {
        "Remote-User": user,
        "Remote-Subject": f"sub-{user}",
        "Remote-Display-Name": user,
        "Remote-Groups": "/apps/customs-invoice",
    }


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
    check(SSO_MODE is True, "已按 SSO 模式加载配置")

    # --- 1. 健康检查免鉴权 ---
    r = client.get("/healthz")
    check(r.status_code == 200 and r.json().get("auth_mode") == "sso",
          "/healthz 免鉴权且报告 sso 模式")

    # --- 2. 未登录页面 → oauth2-proxy 登录入口 ---
    r = client.get("/invoices", headers=HTML)
    location = r.headers.get("location", "")
    check(r.status_code == 303 and location.startswith("/oauth2/start?rd="),
          f"未登录页面跳 oauth2 登录入口（{location}）")

    # --- 3. 前缀不会被加到 /oauth2/ 上 ---
    r = client.get("/invoices", headers={**PREFIX, **HTML})
    location = r.headers.get("location", "")
    check(location.startswith("/oauth2/start?rd=") and "/customs-invoice/oauth2" not in location,
          f"oauth2 端点不被补子路径前缀（{location}）")

    # --- 4. 未登录 API → 401 JSON（不跳转，前端可识别） ---
    r = client.get("/api/invoices/snapshot?ids=1", headers=JSON_ACCEPT)
    check(r.status_code == 401 and r.json().get("code") == 401,
          "未登录 API 返回 401 JSON")

    # --- 5. /login 不再渲染本地登录页 ---
    r = client.get("/login", headers=HTML)
    check(r.status_code == 303 and r.headers.get("location", "").startswith("/oauth2/start"),
          "SSO 模式下 /login 直接跳统一登录")
    r = client.post("/login", data={"username": "admin", "password": "admin123"})
    check(r.status_code == 303 and r.headers.get("location", "").startswith("/oauth2/start"),
          "SSO 模式拒绝本地口令登录")

    # --- 6. 登出：向 Portal 申请完整登出链 ---
    from unittest.mock import patch

    with patch("urllib.request.urlopen", lambda *a, **k: _FakeResponse(
            {"redirect_url": PORTAL_REDIRECT})):
        r = client.get("/logout", headers=_hdr(f"logout_{RUN}"))
    check(r.status_code == 302 and r.headers.get("location") == PORTAL_REDIRECT,
          f"登出跳 Portal 完整登出链（{r.headers.get('location')}）")

    def _boom(*_args: object, **_kwargs: object) -> None:
        """模拟 Portal 不可用。"""
        raise OSError("portal down")

    with patch("urllib.request.urlopen", _boom):
        r = client.get("/logout", headers=_hdr(f"logout_{RUN}"))
    check(r.status_code == 302 and r.headers.get("location") == "/oauth2/sign_out",
          "Portal 不可用时登出回落 oauth2-proxy")

    # --- 7. 改密页改为说明页 ---
    user = f"ssouser_{RUN}"
    r = client.get("/change-password", headers=_hdr(user))
    check(r.status_code == 200 and "统一登录" in r.text, "改密页提示密码由统一登录管理")

    # --- 8. 页面链接全部带子路径前缀 ---
    r = client.get("/details", headers={**PREFIX, **_hdr(user)})
    check(r.status_code == 200, "Remote-* 身份可直接访问业务页")
    check("/customs-invoice/static/css/app.css" in r.text, "静态资源带前缀")
    check('href="/customs-invoice/logout"' in r.text, "退出链接带前缀")
    check('href="/details"' not in r.text, "无根路径绝对链接")

    print()
    if fails:
        print("FAILED:", len(fails))
        for item in fails:
            print("  -", item)
        raise SystemExit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
