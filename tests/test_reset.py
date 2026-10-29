# -*- coding: utf-8 -*-
"""端到端：编辑页一键重置（清空 / 恢复成上个版本）。

覆盖手工验证点：
1. 未生成过报关单 → 重置清空全部填写内容（含单价覆盖）；
2. 一次生成的报关单（只生成过一次）→ 一键清空；
3. 旧版报关单（生成过多次）→ 恢复成上个版本（第 N-1 次生成）的 form_values；
4. 重置落库 + 修改留痕（save 行）+ 快照/预览取到恢复值。
"""

from __future__ import annotations

import json
import re
import sys
import warnings
from pathlib import Path

warnings.simplefilter("ignore")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from webapp.main import app  # noqa: E402

SAMPLE = ROOT / "柬埔寨清关例子" / "VCO_202609_FB675_D202609007.xlsm"

FORM_A = {
    "date": "2026-09-10",
    "bill_no": "2609007",
    "supplier": "VANCO",
    "ship_mode": "by Truck",
    "place_receipt": "SHAOXING,CHINA",
    "port_loading": "HUU NGHI BORDER, VIET NAM",
    "container_info": "SZLU9863221/KAL0586\nPART OF 1X45HQ",
    "flight_no": "",
}
FORM_B = {
    "date": "2026-09-15",
    "bill_no": "2609007",
    "supplier": "VANCO",
    "ship_mode": "by Truck",
    "place_receipt": "NINGBO,CHINA",
    "port_loading": "",
    "container_info": "TEMU9126356/SEAL999",
    "flight_no": "",
    "unit_prices": '{"JC": 2.7}',
}
FORM_C = {
    "date": "2026-09-20",
    "bill_no": "2609007C",
    "supplier": "EASTEX",
    "ship_mode": "by Sea",
    "place_receipt": "SHANGHAI,CHINA",
    "port_loading": "",
    "container_info": "FBLU0186990/SEAL123",
    "flight_no": "",
}


def _csrf_from(html: str) -> str:
    m = re.search(r'name="csrf"\s+value="([^"]+)"', html)
    assert m, "未找到 CSRF token"
    return m.group(1)


def _all_empty(values: dict) -> bool:
    return all(str(v or "") == "" for v in values.values())


def main() -> None:
    from webapp.models import ExportFileVersion as _EV, Invoice as _Invoice
    from webapp.models import SessionLocal as _SL

    # 清理历史测试数据，保证生成次数可断言
    db = _SL()
    db.query(_EV).delete()
    inv = db.query(_Invoice).filter(_Invoice.invoice_no == "D202609007").first()
    if inv is not None:
        inv.manual_values = None
        inv.manual_saved_by = None
        inv.manual_saved_at = None
        db.commit()
    db.close()

    client = TestClient(app, base_url="http://testserver", follow_redirects=False)
    r = client.post(
        "/login",
        data={"username": "admin", "password": "admin123", "next": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code

    # ---- 准备：导入样例 ----
    csrf = _csrf_from(client.get("/invoices").text)
    with SAMPLE.open("rb") as fh:
        r = client.post(
            "/admin/upload",
            data={"csrf": csrf},
            files=[("files", (SAMPLE.name, fh, "application/octet-stream"))],
        )
    if "Duplicate Invoice No." in r.text:
        csrf = _csrf_from(r.text)
        r = client.post(
            "/admin/upload",
            data={
                "csrf": csrf,
                "confirm": "1",
                "pending_json": json.dumps(
                    [{
                        "file": SAMPLE.name,
                        "path": str(SAMPLE),
                        "invoice_no": "D202609007",
                        "overwrite": True,
                    }]
                ),
            },
        )
    assert r.status_code == 200, r.text[:300]

    db = _SL()
    inv = db.query(_Invoice).filter(_Invoice.invoice_no == "D202609007").first()
    assert inv is not None, "样例发票应存在"
    inv_id = inv.id
    db.close()
    ids = str(inv_id)

    # ---- 1. 尚未生成：重置计划 = 清空；保存后一键清空 ----
    r = client.post(
        "/api/invoices/manual-save",
        params={"ids": ids},
        json={"form": FORM_A},
    )
    assert r.status_code == 200 and r.json()["ok"], r.text[:300]

    r = client.get("/api/invoices/reset-plan", params={"ids": ids})
    plan = r.json()
    assert plan["ok"] and plan["mode"] == "clear", plan
    assert plan["generations"] == 0 and plan["latest_version"] is None, plan
    print("[OK] 未生成过报关单 → 重置计划为清空")

    r = client.post("/api/invoices/reset", params={"ids": ids})
    d = r.json()
    assert d["ok"] and d["mode"] == "clear", d
    assert _all_empty(d["values"]), d["values"]
    assert d["target_version"] is None, d
    print("[OK] 一键清空:", d["message"])

    r = client.get("/api/invoices/snapshot", params={"ids": ids})
    snap = r.json()
    assert snap["ok"] and (snap.get("manual") or {}) == {}, snap.get("manual")
    print("[OK] 清空后快照 manual 为空")

    # ---- 2. 一次生成的报关单：仍为清空 ----
    r = client.post(
        "/api/invoices/export", params={"ids": ids}, json={"form": FORM_A}
    )
    assert r.status_code == 200 and r.json()["ok"], r.text[:500]
    v1 = r.json()["version"]

    r = client.get("/api/invoices/reset-plan", params={"ids": ids})
    plan = r.json()
    assert plan["mode"] == "clear" and plan["generations"] == 1, plan
    assert plan["latest_version"] == v1, plan

    r = client.post("/api/invoices/reset", params={"ids": ids})
    d = r.json()
    assert d["ok"] and d["mode"] == "clear" and _all_empty(d["values"]), d
    assert "only been generated once" in d["message"], d["message"]
    print("[OK] 一次生成的报关单 → 一键清空全部填写内容")

    # ---- 3. 旧版报关单：恢复成上个版本（第 N-1 次生成）----
    r = client.post(
        "/api/invoices/export", params={"ids": ids}, json={"form": FORM_B}
    )
    assert r.status_code == 200 and r.json()["ok"], r.text[:500]
    v2 = r.json()["version"]
    assert v2 == v1 + 1, (v1, v2)

    # 再改一版内容（仅保存不生成），模拟「修改内容」
    r = client.post(
        "/api/invoices/manual-save",
        params={"ids": ids},
        json={"form": FORM_C},
    )
    assert r.status_code == 200 and r.json()["ok"], r.text[:300]

    r = client.get("/api/invoices/reset-plan", params={"ids": ids})
    plan = r.json()
    assert plan["mode"] == "restore", plan
    assert plan["generations"] == 2, plan
    assert plan["target_version"] == v1 and plan["latest_version"] == v2, plan
    print("[OK] 旧版报关单 → 重置计划为恢复上个版本（第", plan["target_version"], "次生成）")

    r = client.post("/api/invoices/reset", params={"ids": ids})
    d = r.json()
    assert d["ok"] and d["mode"] == "restore", d
    assert d["target_version"] == v1 and d["latest_version"] == v2, d
    values = d["values"]
    # 修改内容整体回到上个版本（第 1 次生成），而不是只撤销最近一次改动
    assert values["date"] == FORM_A["date"], values
    assert values["place_receipt"] == FORM_A["place_receipt"], values
    assert values["container_info"] == FORM_A["container_info"], values
    assert values["supplier"] == FORM_A["supplier"], values
    assert values["port_loading"] == FORM_A["port_loading"], values
    assert values["unit_prices"] == "", values  # 第 1 次生成没有单价覆盖 → 恢复为空
    print("[OK] 恢复值 = 第 1 次生成保存的 form_values（整体回到上个版本）")

    r = client.get("/api/invoices/snapshot", params={"ids": ids})
    snap = r.json()
    manual = snap.get("manual") or {}
    assert manual.get("date") == FORM_A["date"], manual
    assert manual.get("container_info") == FORM_A["container_info"], manual
    assert "unit_prices" not in manual, manual
    print("[OK] 恢复后快照 manual 取到上个版本的值")

    db = _SL()
    from webapp.models import ExportFileVersion as _EV2

    save_rows = (
        db.query(_EV2)
        .filter(_EV2.kind == "save", _EV2.folder == "")
        .order_by(_EV2.id.asc())
        .all()
    )
    assert save_rows, "重置应留下 save 修改留痕"
    last_snapshot = json.loads(save_rows[-1].form_values or "{}")
    assert last_snapshot.get("date") == FORM_A["date"], last_snapshot
    assert last_snapshot.get("container_info") == FORM_A["container_info"], last_snapshot
    db.close()
    print("[OK] 重置留下 save 修改留痕")

    # ---- 4. 三次生成后：上个版本 = 第 2 次生成（不是最初版本）----
    r = client.post(
        "/api/invoices/export", params={"ids": ids}, json={"form": FORM_C}
    )
    assert r.status_code == 200 and r.json()["ok"], r.text[:500]
    v3 = r.json()["version"]

    r = client.get("/api/invoices/reset-plan", params={"ids": ids})
    plan = r.json()
    assert plan["mode"] == "restore", plan
    assert plan["target_version"] == v2 and plan["latest_version"] == v3, plan

    r = client.post("/api/invoices/reset", params={"ids": ids})
    d = r.json()
    assert d["ok"] and d["values"]["date"] == FORM_B["date"], d
    assert d["values"]["container_info"] == FORM_B["container_info"], d
    print("[OK] 第 3 次生成后重置 → 恢复到第 2 次生成（上个版本），非最初版本")

    print("\n编辑页一键重置测试全部通过")


if __name__ == "__main__":
    main()
