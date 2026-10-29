# -*- coding: utf-8 -*-
"""端到端：Shipment List 行内编辑 → 同步 .xlsm / 修改记录 / 发票明细。

覆盖计划中的手工验证点：
1. 改柜号/日期/Package/GW/NW → .xlsm 对应单元格改写 + file_history 留底 +
   修改记录时间线出现「编辑保存」含 old→new + 发票明细 Container No. 同步；
2. 改 ETD 等平台-only 字段 → 不碰文件、不插修改记录、月度 Excel 照常重建；
3. 改 ID → 明细页 ID 实时映射生效；
4. 平台改写后的文件回传校验不误报（比对带 sha256 的 save 行）；
5. 批量下载 zip 含 .xlsm。
"""

from __future__ import annotations

import io
import json
import re
import sys
import warnings
import zipfile
from pathlib import Path

warnings.simplefilter("ignore")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from webapp.main import app  # noqa: E402

SAMPLE = ROOT / "柬埔寨清关例子" / "VCO_202609_FB675_D202609007.xlsm"


def _csrf_from(html: str) -> str:
    m = re.search(r'name="csrf"\s+value="([^"]+)"', html)
    assert m, "未找到 CSRF token"
    return m.group(1)


def _xlsm_cells(path: Path) -> dict:
    """读取 .xlsm 关键单元格（柜号区/Date/Mode/PKL1/G 列合计）。"""
    import openpyxl

    wb = openpyxl.load_workbook(str(path), keep_vba=False)
    ws = wb["INV1"]
    container_row = None
    for r in range(24, 60):
        a_val = ws[f"A{r}"].value
        if a_val and "CONTAINER" in str(a_val).upper():
            container_row = r
            break
    cells = {
        "J9": ws["J9"].value,
        "B17": ws["B17"].value,
        "A18": ws["A18"].value,
        "containers": [
            ws[f"A{container_row + 1 + i}"].value if container_row else None
            for i in range(4)
        ],
    }
    if "PKL1" in wb.sheetnames:
        cells["PKL1_A24"] = wb["PKL1"]["A24"].value
        cells["PKL1_K24"] = wb["PKL1"]["K24"].value
    # G 列报关重量合计（面料行 = 24..Total 行-1，与改写分摊口径一致）
    total_row = 37
    for r in range(24, 60):
        c_val = ws[f"C{r}"].value
        if c_val and "Total" in str(c_val):
            total_row = r
            break
    g_sum = 0.0
    for r in range(24, total_row):
        val = ws[f"G{r}"].value
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            g_sum += float(val)
    cells["G_sum"] = g_sum
    wb.close()
    return cells


def main() -> None:
    from webapp.models import (
        ExportFileVersion,
        ExportJob,
        Invoice,
        SessionLocal,
        ShipmentListEntry,
    )

    client = TestClient(app, base_url="http://testserver", follow_redirects=False)
    r = client.post(
        "/login",
        data={"username": "admin", "password": "admin123", "next": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.status_code

    # ---- 准备：导入样例 + 导出生成报关发票与装柜行 ----
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
    assert r.status_code == 200 and r.json()["ok"], r.text[:500]
    exported = r.json()
    folder, filename = exported["folder"], exported["filename"]
    out_path = Path(exported["path"])
    assert out_path.exists()

    db = SessionLocal()
    job = db.query(ExportJob).order_by(ExportJob.id.desc()).first()
    entry = (
        db.query(ShipmentListEntry)
        .filter(ShipmentListEntry.job_id == job.id)
        .order_by(ShipmentListEntry.id.desc())
        .first()
    )
    assert entry is not None, "导出后应有装柜记录"
    entry_id = entry.id

    hist_dir = ROOT / "data" / "file_history" / folder
    hist_before = len(list(hist_dir.glob("*.xlsm"))) if hist_dir.exists() else 0

    # ---- 1. 行内编辑：柜号/日期/Package/GW/NW/ETD/ID 等全量提交 ----
    form = {
        "date": "2026-09-12",
        "fty_inv": entry.fty_inv or "",
        "shipper": entry.shipper or "",
        "est_vco": entry.est_vco or "",
        "custom_no": entry.custom_no or "",
        "package": "50",
        "blading": entry.blading or "",
        "container_1": "NEWCONT1/SEAL1",
        "container_2": "",
        "container_3": "",
        "container_4": "",
        "nw_kg": "1234",
        "gw_kg": "2000",
        "description": entry.description or "",
        "mode": "SEA",
        "etd": "2026-09-20",
        "eta": "",
        "pol": "NINGBO, CHINA",
        "vessel": "NEW VESSEL",
        "carrier": entry.carrier or "",
        "container_type": entry.container_type or "",
        "cbm": "",
        "ref_id": "TEST-001",
    }
    r = client.post(f"/api/shipments/{entry_id}/update", data=form)
    assert r.status_code == 200, r.text[:500]
    result = r.json()
    assert result["ok"], result
    assert result["xlsm_synced"] is True, result
    print("[OK] 行内编辑保存:", result["message"])

    db.expire_all()
    entry = db.get(ShipmentListEntry, entry_id)
    assert entry.date == "2026-09-12", entry.date
    assert entry.container_1 == "NEWCONT1/SEAL1"
    assert (entry.container_2 or "") == ""
    assert entry.eta == "2026-09-28", entry.eta  # SEA: ETD+8 自动推算
    assert entry.vessel == "NEW VESSEL" and entry.ref_id == "TEST-001"
    assert float(entry.nw_kg) == 1234 and float(entry.gw_kg) == 2000
    assert entry.package == 50
    print("[OK] 落库 + ETA 自动推算 (ETD 2026-09-20 → ETA 2026-09-28)")

    cells = _xlsm_cells(out_path)
    assert cells["J9"] == "2026-09-12", cells["J9"]
    assert cells["B17"] == "by Sea", cells["B17"]
    assert cells["containers"][0] == "NEWCONT1/SEAL1", cells["containers"]
    assert cells["containers"][1] in (None, ""), cells["containers"]
    assert cells["PKL1_A24"] == 50, cells["PKL1_A24"]
    assert float(cells["PKL1_K24"]) == 2000, cells["PKL1_K24"]
    assert abs(cells["G_sum"] - 1234) < 0.01, cells["G_sum"]
    print("[OK] .xlsm 改写: J9/B17/柜号区/PKL1 A24+K24/G 列合计", cells["G_sum"])

    hist_after = len(list(hist_dir.glob("*.xlsm"))) if hist_dir.exists() else 0
    assert hist_after == hist_before + 1, (hist_before, hist_after)
    print("[OK] 旧文件已留底 file_history +1")

    save_row = (
        db.query(ExportFileVersion)
        .filter(ExportFileVersion.kind == "save", ExportFileVersion.folder == folder)
        .order_by(ExportFileVersion.id.desc())
        .first()
    )
    assert save_row is not None, "应有改写留痕 save 行"
    assert save_row.sha256 and save_row.backup_path, save_row.__dict__
    assert Path(save_row.backup_path).exists()
    snapshot = json.loads(save_row.form_values or "{}")
    assert snapshot.get("container_info") == "NEWCONT1/SEAL1", snapshot
    assert snapshot.get("date") == "2026-09-12", snapshot
    print("[OK] 修改记录 save 行：sha256 + 留底 + 快照")

    r = client.get("/api/files/history", params={"folder": folder, "name": filename})
    assert r.status_code == 200 and r.json()["ok"], r.text[:300]
    rows = r.json()["rows"]
    last = rows[-1]
    assert last["kind"] == "save", last
    assert last["backup"], "save 行带留底下载"
    changed_labels = {c["label"] for c in last["changes"]}
    assert "Container 1" in changed_labels, changed_labels
    assert "Date" in changed_labels, changed_labels
    assert any(c["old"] and c["new"] for c in last["changes"]), last["changes"]
    print("[OK] 历史时间线：编辑保存 + 字段 old→new:", changed_labels)

    # 发票明细 Container No. / ID 同步
    invoice = db.get(Invoice, 1)
    manual = json.loads(invoice.manual_values or "{}")
    assert manual.get("container_info") == "NEWCONT1/SEAL1", manual
    print("[OK] 发票明细 manual_values 柜号已同步")
    r = client.get("/details", params={"invoice_id": 1})
    assert r.status_code == 200
    assert "NEWCONT1/SEAL1" in r.text and "TEST-001" in r.text, "明细页展示新柜号与 ID"
    print("[OK] 明细页 Container No. 与 ID 生效")

    # ---- 2. 平台-only 字段（ETD/VESSEL）：不碰文件、不插记录 ----
    saves_before = (
        db.query(ExportFileVersion)
        .filter(ExportFileVersion.kind == "save", ExportFileVersion.folder == folder)
        .count()
    )
    form2 = dict(form)
    form2["etd"] = "2026-09-25"
    form2["eta"] = "2026-10-01"
    form2["vessel"] = "OTHER VESSEL"
    r = client.post(f"/api/shipments/{entry_id}/update", data=form2)
    assert r.status_code == 200 and r.json()["ok"], r.text[:300]
    assert r.json()["xlsm_synced"] is False, r.json()
    saves_after = (
        db.query(ExportFileVersion)
        .filter(ExportFileVersion.kind == "save", ExportFileVersion.folder == folder)
        .count()
    )
    assert saves_after == saves_before, (saves_before, saves_after)
    assert _xlsm_cells(out_path)["containers"][0] == "NEWCONT1/SEAL1"
    db.expire_all()
    entry = db.get(ShipmentListEntry, entry_id)
    assert entry.etd == "2026-09-25" and entry.eta == "2026-10-01"
    print("[OK] ETD/ETA/VESSEL 平台-only：文件不动、无新记录")

    # ---- 3. 回传校验：平台改写后的文件与 save 行一致（不误报） ----
    with out_path.open("rb") as fh:
        r = client.post(
            "/api/files/verify",
            files=[("file", (filename, fh, "application/octet-stream"))],
        )
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    assert data["matched"] is True, data
    assert "edit-save version" in data["message"], data["message"]
    print("[OK] 校验：改写后的文件与平台 save 记录一致")

    # ---- 4. 批量下载 zip ----
    r = client.get("/api/shipments/download-files", params={"ids": str(entry_id)})
    assert r.status_code == 200, r.text[:300]
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert filename in names, names
    assert "missing.txt" not in names
    print("[OK] 批量下载 zip 含", names)

    # ---- 5. 月度 Excel 照常重建 ----
    from core.shipment_list import month_path

    month_file = month_path(entry.month)
    assert month_file.exists(), month_file
    print("[OK] 月度 Excel 存在:", month_file.name)

    db.close()
    print("\nShipment List 行内编辑三路同步测试全部通过")


if __name__ == "__main__":
    main()
