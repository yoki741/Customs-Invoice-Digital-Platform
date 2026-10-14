# -*- coding: utf-8 -*-
"""端到端冒烟测试：登录 → 导入 → 列表 → 快照 → 导出。"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

warnings.simplefilter("ignore")
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from webapp.main import app  # noqa: E402

SAMPLE = ROOT / "柬埔寨清关例子" / "VCO_202609_FB675_D202609007.xlsm"


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

    with SAMPLE.open("rb") as fh:
        r = client.post(
            "/admin/upload",
            files=[("files", (SAMPLE.name, fh, "application/octet-stream"))],
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

    print("\n全部冒烟测试通过")


if __name__ == "__main__":
    main()
