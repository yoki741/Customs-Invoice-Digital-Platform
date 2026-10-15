# -*- coding: utf-8 -*-
"""端到端冒烟测试：登录 → 导入 → 列表 → 快照 → 导出。"""

from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

warnings.simplefilter("ignore")
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from webapp.main import app  # noqa: E402

SAMPLE = ROOT / "柬埔寨清关例子" / "VCO_202609_FB675_D202609007.xlsm"


def _csrf_from(html: str) -> str:
    import re
    m = re.search(r'name="csrf"\s+value="([^"]+)"', html)
    assert m, "未找到 CSRF token"
    return m.group(1)


def main() -> None:
    client = TestClient(app, base_url="http://testserver", follow_redirects=False)

    r = client.get("/login")
    assert r.status_code == 200, r.status_code

    r = client.post(
        "/login",
        data={"username": "admin", "password": "admin123", "next": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303, (r.status_code, r.text[:200])
    print("[OK] 登录 ->", r.headers["location"])

    r = client.get("/admin/upload")
    assert r.status_code == 200, r.status_code
    csrf = _csrf_from(r.text)

    with SAMPLE.open("rb") as fh:
        r = client.post(
            "/admin/upload",
            data={"csrf": csrf},
            files=[("files", (SAMPLE.name, fh, "application/octet-stream"))],
        )
    assert r.status_code == 200, (r.status_code, r.text[:500])

    # 已存在同号发票时走两段式确认：勾选覆盖后提交
    if "覆盖" in r.text or "确认" in r.text:
        csrf = _csrf_from(r.text)
        r = client.post(
            "/admin/upload",
            data={"csrf": csrf, "confirm": "1",
                  "pending_json": json.dumps([{
                      "file": SAMPLE.name, "path": str(SAMPLE),
                      "invoice_no": "D202609007", "overwrite": True,
                  }])},
        )
        assert r.status_code == 200, (r.status_code, r.text[:500])

    assert "成功" in r.text, r.text[:500]
    print("[OK] 上传导入")

    r = client.get("/invoices")
    assert r.status_code == 200, r.status_code
    assert "D202609007" in r.text
    print("[OK] 发票列表")

    r = client.get("/api/invoices/snapshot", params={"ids": "1"})
    assert r.status_code == 200, (r.status_code, r.text[:500])
    data = r.json()
    assert data["ok"], data
    sheets = list(data["snapshot"]["sheets"].values())
    print("[OK] 快照:", [s["name"] for s in sheets], "| 可编辑:", data["editable"])

    r = client.get("/api/invoices/preview.xlsx", params={"ids": "1"})
    assert r.status_code == 200, (r.status_code, r.text[:300])
    print("[OK] 预填 xlsx:", len(r.content), "bytes")

    payload = {
        "form": {
            "date": "2026-09-10",
            "bill_no": "2609007",
            "supplier": "VANCO",
            "ship_mode": "by Truck",
            "place_receipt": "SHAOXING,CHINA",
            "port_loading": "HUU NGHI BORDER, VIET NAM",
            "container_info": "SZLU9863221/KAL0586\nPART OF 1X45HQ",
            "flight_no": "",
        }
    }
    r = client.post("/api/invoices/export", params={"ids": "1"}, json=payload)
    assert r.status_code == 200, (r.status_code, r.text[:500])
    exported = r.json()
    assert exported["ok"], exported
    print("[OK] 导出:", exported["filename"], exported["customs_weight"], exported["total_amount"])

    out = Path(exported["path"])
    assert out.exists(), out
    print("[OK] 文件存在:", out, out.stat().st_size, "bytes")

    r = client.get(exported["download"])
    assert r.status_code == 200, r.status_code
    print("[OK] 下载:", len(r.content), "bytes")

    r = client.get("/details")
    assert r.status_code == 200, r.status_code
    print("[OK] 明细页")

    r = client.get("/admin/exports")
    assert r.status_code == 200, r.status_code
    print("[OK] 导出历史")

    r = client.get("/admin/users")
    assert r.status_code == 200, r.status_code
    print("[OK] 账号管理")

    # --- 审计日志 ---
    r = client.get("/admin/audit")
    assert r.status_code == 200, r.status_code
    assert "操作日志" in r.text
    r = client.get("/admin/audit", params={"action": "export"})
    assert r.status_code == 200 and "export" in r.text
    print("[OK] 审计日志 + 按操作筛选")

    # --- 自助改密码 ---
    r = client.get("/change-password")
    assert r.status_code == 200 and "修改我的密码" in r.text
    csrf = _csrf_from(r.text)
    r = client.post("/change-password", data={
        "csrf": csrf, "old_password": "wrong", "new_password": "newpass123",
        "confirm_password": "newpass123"}, follow_redirects=False)
    assert r.status_code == 303, r.status_code
    r = client.get("/change-password")
    assert "原密码错误" in r.text
    r = client.post("/change-password", data={
        "csrf": _csrf_from(r.text), "old_password": "admin123",
        "new_password": "admin123", "confirm_password": "admin123"},
        follow_redirects=False)
    r = client.get("/change-password")
    assert "不能与原密码相同" in r.text
    r = client.post("/change-password", data={
        "csrf": _csrf_from(r.text), "old_password": "admin123",
        "new_password": "admin456", "confirm_password": "admin456"},
        follow_redirects=False)
    assert r.status_code == 303
    r = client.get("/change-password")
    assert "密码已修改" in r.text
    print("[OK] 自助改密码（错误/相同/成功）")

    # 审计里应有改密码记录
    r = client.get("/admin/audit", params={"action": "password.change"})
    assert r.status_code == 200 and "修改了自己的密码" in r.text
    print("[OK] 改密码已记入审计日志")

    # 改回原密码
    r = client.post("/change-password", data={
        "csrf": _csrf_from(client.get("/change-password").text),
        "old_password": "admin456", "new_password": "admin123",
        "confirm_password": "admin123"}, follow_redirects=False)
    assert r.status_code == 303

    # --- 日期范围筛选 ---
    r = client.get("/invoices", params={"date_from": "2099-01-01"})
    assert r.status_code == 200 and "暂无数据" in r.text, r.text[:300]
    r = client.get("/invoices", params={"date_from": "2000-01-01",
                                        "date_to": "2099-12-31"})
    assert "D202609007" in r.text
    r = client.get("/invoices", params={"date_from": "bad-date"})
    assert "D202609007" in r.text  # 非法日期忽略
    print("[OK] 日期范围筛选（空/全含/非法忽略）")

    # --- 问题说明可见性（先把发票标记为有问题） ---
    r = client.get("/details?invoice_id=1")
    csrf = _csrf_from(r.text)
    client.post("/details/1/check", data={
        "csrf": csrf, "status": "failed", "note": "冒烟测试问题说明"},
        follow_redirects=False)
    r = client.get("/invoices")
    assert "冒烟测试问题说明" in r.text, "列表页应直接显示问题说明"
    print("[OK] 列表页直接显示问题说明")

    print("\n全部冒烟测试通过")


if __name__ == "__main__":
    main()
