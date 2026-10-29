# -*- coding: utf-8 -*-
"""端到端：Shipment List 权限分割。

覆盖手工验证点：
1. 报关发票列 = 第一列且冻结（sticky-col）；只有第二类账号（及管理员）能从这里
   点击进入编辑页，且进入的是第一次生成该报关发票的页面（invoice_ids 取首次
   生成记录，不是最新一次）；
2. ID（ref_id）只有第三类账号 / 管理员可改：第三类可写，第一类提交被忽略
   （保留原值），第一类直调 /ref-id 接口被拒，第二类不能行内编辑；
3. 机器柜 / 辅料柜只在 Shipment List 页面行内编辑：可写、随月度 Excel 输出、
   编辑页弹窗不提供、重新导出不覆盖页面手填值。
"""

from __future__ import annotations

import json
import re
import sys
import warnings
from pathlib import Path
from typing import Any

warnings.simplefilter("ignore")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from webapp.main import app  # noqa: E402

SAMPLE = ROOT / "柬埔寨清关例子" / "VCO_202609_FB675_D202609007.xlsm"
TMP_PW = "tmp-perm-12345"
FAKE_FIRST_IDS = "777"

FORM = {
    "date": "2026-09-10",
    "bill_no": "2609007",
    "supplier": "VANCO",
    "ship_mode": "by Truck",
    "place_receipt": "SHAOXING,CHINA",
    "port_loading": "HUU NGHI BORDER, VIET NAM",
    "container_info": "SZLU9863221/KAL0586\nPART OF 1X45HQ",
    "flight_no": "",
}


def _csrf_from(html: str) -> str:
    m = re.search(r'name="csrf"\s+value="([^"]+)"', html)
    assert m, "未找到 CSRF token"
    return m.group(1)


def _login(username: str, password: str) -> TestClient:
    client = TestClient(app, base_url="http://testserver", follow_redirects=False)
    r = client.post(
        "/login",
        data={"username": username, "password": password, "next": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303, f"{username} 登录失败: {r.status_code}"
    return client


def _row_form(entry: Any, **overrides: str) -> dict[str, str]:
    """按当前库内值构造行内编辑全量提交（空值 = 清空，不能漏字段）。"""
    from webapp.service import ENTRY_EDIT_FIELDS

    form: dict[str, str] = {}
    for f in ENTRY_EDIT_FIELDS:
        v = getattr(entry, f)
        form[f] = "" if v is None else str(v)
    form.update(overrides)
    return form


def _check_first_generation() -> None:
    """first_generation_ids 单元语义：同文件取最早一条 export，忽略 save / 其它文件。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from webapp.models import Base, ExportFileVersion
    from webapp.service import first_generation_ids

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add_all(
        [
            ExportFileVersion(kind="save", folder="F", filename="A.xlsm", invoice_ids="[5]"),
            ExportFileVersion(kind="export", folder="F", filename="A.xlsm", invoice_ids="[9]"),
            ExportFileVersion(kind="export", folder="F", filename="A.xlsm", invoice_ids="[1,2]"),
            ExportFileVersion(kind="export", folder="F", filename="B.xlsm", invoice_ids="[7]"),
        ]
    )
    db.commit()
    assert first_generation_ids(db, "F", "A.xlsm") == "9", "应取第一次生成的 invoice_ids"
    assert first_generation_ids(db, "F", "B.xlsm") == "7", "按文件名分别取首次生成"
    assert first_generation_ids(db, "F", "Z.xlsm") == "", "无生成记录返回空"
    assert first_generation_ids(db, "F", "") == "", "空文件名返回空"
    print("[OK] first_generation_ids 取第一次生成（忽略 save 行与其它文件）")
    db.close()
    engine.dispose()


def main() -> None:
    from webapp.auth import hash_password
    from webapp.models import (
        ROLE_TYPE1,
        ROLE_TYPE2,
        ROLE_TYPE3,
        SessionLocal,
        User,
    )

    _check_first_generation()

    db = SessionLocal()
    # 临时账号（测试结束删除，不动真实账号）
    db.query(User).filter(User.username.like("_perm_test_%")).delete()
    db.commit()
    users = {}
    for role, name in (
        (ROLE_TYPE1, "_perm_test_t1"),
        (ROLE_TYPE2, "_perm_test_t2"),
        (ROLE_TYPE3, "_perm_test_t3"),
    ):
        u = User(
            username=name,
            password_hash=hash_password(TMP_PW),
            role=role,
            display_name=name,
        )
        db.add(u)
        users[role] = name
    db.commit()

    try:
        _run(users, db)
    finally:
        db.expire_all()
        db.query(User).filter(User.username.like("_perm_test_%")).delete()
        db.commit()
        db.close()
    print("\nShipment List 权限分割测试全部通过")


def _run(users: dict[str, str], db: Any) -> None:
    from webapp.models import ExportFileVersion, ExportJob, Invoice, ShipmentListEntry
    from webapp.service import first_generation_ids, month_path

    admin = _login("admin", "admin123")
    c1 = _login(users["type1"], TMP_PW)
    c2 = _login(users["type2"], TMP_PW)
    c3 = _login(users["type3"], TMP_PW)

    # ---- 准备：导入样例 + 导出生成 ----
    csrf = _csrf_from(admin.get("/invoices").text)
    with SAMPLE.open("rb") as fh:
        r = admin.post(
            "/admin/upload",
            data={"csrf": csrf},
            files=[("files", (SAMPLE.name, fh, "application/octet-stream"))],
        )
    if "Duplicate Invoice No." in r.text:
        csrf = _csrf_from(r.text)
        r = admin.post(
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

    inv = db.query(Invoice).filter(Invoice.invoice_no == "D202609007").first()
    assert inv is not None, "样例发票应存在"
    inv_id = inv.id

    r = admin.post("/api/invoices/export", params={"ids": str(inv_id)}, json={"form": FORM})
    assert r.status_code == 200 and r.json()["ok"], r.text[:500]
    folder, filename = r.json()["folder"], r.json()["filename"]
    assert Path(r.json()["path"]).exists()

    # 清掉本次之前的生成记录，插入一条“更早的第一次生成”（invoice_ids=777），
    # 后续两次真实导出都晚于它 → 编辑入口必须落到它（第一次生成），而不是最新
    db.query(ExportFileVersion).filter(ExportFileVersion.folder == folder).delete()
    db.add(
        ExportFileVersion(
            kind="export",
            version=1,
            folder=folder,
            filename=filename,
            invoice_ids=f"[{FAKE_FIRST_IDS}]",
            form_values="{}",
        )
    )
    db.commit()

    for _ in range(2):
        r = admin.post("/api/invoices/export", params={"ids": str(inv_id)}, json={"form": FORM})
        assert r.status_code == 200 and r.json()["ok"], r.text[:500]

    job = db.query(ExportJob).order_by(ExportJob.id.desc()).first()
    entry = (
        db.query(ShipmentListEntry)
        .filter(ShipmentListEntry.job_id == job.id)
        .order_by(ShipmentListEntry.id.desc())
        .first()
    )
    assert entry is not None, "导出后应有装柜记录"
    entry_id = entry.id
    assert first_generation_ids(db, folder, filename) == FAKE_FIRST_IDS
    print("[OK] 准备完成：2 次真实生成 + 1 条更早的首次生成记录")

    # ---- 1. 报关发票列 = 第一列 + 冻结；只有第二类（及管理员）从此进入编辑 ----
    page = admin.get("/shipments").text
    assert page.index('class="sticky-col">Customs Invoice') < page.index("<th>Date</th>"), \
        "报关发票必须是第一列表头"
    assert "sticky-col" in page, "冻结列样式缺失"
    assert f"/invoices/edit?ids={FAKE_FIRST_IDS}" in page, "管理员编辑入口指向第一次生成"
    assert "Edit Invoice" in page and "previewFromLink" in page, "管理员可预览可编辑"
    print("[OK] 管理员视角：报关发票第一列且冻结，编辑入口 = 第一次生成的页面")

    r = c2.get("/shipments")
    assert r.status_code == 200, r.status_code
    text2 = r.text
    assert f"/invoices/edit?ids={FAKE_FIRST_IDS}" in text2, "第二类从报关发票列进入编辑"
    assert 'title="Edit customs invoice' in text2
    assert "Download Selected" not in text2, "第二类无批量下载"
    assert 'onclick="editRow(this)"' not in text2, "第二类无行内编辑"
    assert "file-check" not in text2.split("<script")[0], "第二类无下载勾选框"
    print("[OK] 第二类视角：文件名即编辑链接（第一次生成），无下载/行内编辑")

    r = c3.get("/shipments")
    assert r.status_code == 200, r.status_code
    text3 = r.text
    assert "/invoices/edit?ids=" not in text3, "第三类无编辑入口（只预览）"
    assert "previewFromLink" in text3 and "Download Selected" in text3
    assert 'onclick="editRow(this)"' in text3, "第三类可行内编辑"
    print("[OK] 第三类视角：仅预览，不提供报关发票编辑入口")

    r = c2.get(f"/invoices/edit?ids={inv_id}")
    assert r.status_code == 200, r.status_code
    r = c2.get("/api/invoices/snapshot", params={"ids": str(inv_id)})
    assert r.status_code == 200 and r.json()["ok"], r.text[:300]
    r = c3.get(f"/invoices/edit?ids={inv_id}")
    assert r.status_code == 403, r.status_code
    print("[OK] 编辑页权限：第二类可进（快照可用），第三类 403")

    # ---- 2. ID 字段仅第三类 / 管理员可改 ----
    form = _row_form(entry, ref_id="T3-ID-001")
    r = c3.post(f"/api/shipments/{entry_id}/update", data=form)
    assert r.status_code == 200 and r.json()["ok"], r.text[:300]
    db.expire_all()
    entry = db.get(ShipmentListEntry, entry_id)
    assert entry.ref_id == "T3-ID-001", entry.ref_id
    print("[OK] 第三类行内编辑 ID 生效")

    form = _row_form(entry, ref_id="HACK-ID", vessel="PERM VESSEL")
    r = c1.post(f"/api/shipments/{entry_id}/update", data=form)
    assert r.status_code == 200 and r.json()["ok"], r.text[:300]
    db.expire_all()
    entry = db.get(ShipmentListEntry, entry_id)
    assert entry.ref_id == "T3-ID-001", f"第一类不得改动 ID，实际 {entry.ref_id}"
    assert entry.vessel == "PERM VESSEL", "第一类其它字段照常可写"
    print("[OK] 第一类提交 ID 被忽略（保留原值），其它字段正常保存")

    r = c1.post(f"/api/shipments/{entry_id}/ref-id", data={"ref_id": "HACK-2"})
    assert r.status_code == 403, r.status_code
    r = c2.post(f"/api/shipments/{entry_id}/update", data=_row_form(entry, ref_id="X"))
    assert r.status_code == 403, r.status_code
    r = c3.post(f"/api/shipments/{entry_id}/ref-id", data={"ref_id": "T3-ID-002"})
    assert r.status_code == 303, r.status_code
    db.expire_all()
    entry = db.get(ShipmentListEntry, entry_id)
    assert entry.ref_id == "T3-ID-002", entry.ref_id
    print("[OK] /ref-id 接口仅第三类（第一类 403、第二类行内编辑 403）")

    page = admin.get(f"/invoices/edit?ids={inv_id}").text
    assert 'id="sm-ref-id"' in page, "管理员弹窗有 ID 输入"
    assert "CAN_SHIPMENT_INFO = true" in page
    page = c1.get(f"/invoices/edit?ids={inv_id}").text
    assert 'id="sm-ref-id"' not in page, "第一类弹窗无 ID 输入"
    page = c2.get(f"/invoices/edit?ids={inv_id}").text
    assert 'id="sm-ref-id"' not in page, "第二类弹窗无 ID 输入"
    assert "CAN_SHIPMENT_INFO = false" in page, "第二类生成后不弹装柜补充信息窗口"
    print("[OK] 编辑页弹窗：ID 仅第三类/管理员可见，第二类不弹窗")

    r = c1.post("/api/shipments/info", data={"ids": str(inv_id), "ref_id": "SNEAK-ID"})
    assert r.status_code == 200 and r.json()["ok"], r.text[:300]
    db.expire_all()
    entry = db.get(ShipmentListEntry, entry_id)
    assert entry.ref_id == "T3-ID-002", f"补充信息窗口也不得改 ID，实际 {entry.ref_id}"
    r = c3.post("/api/shipments/info", data={"ids": str(inv_id), "ref_id": "T3-ID-003"})
    assert r.status_code == 200 and r.json()["ok"], r.text[:300]
    db.expire_all()
    entry = db.get(ShipmentListEntry, entry_id)
    assert entry.ref_id == "T3-ID-003", entry.ref_id
    r = c2.post("/api/shipments/info", data={"ids": str(inv_id), "ref_id": "T2-ID"})
    assert r.status_code == 403, r.status_code
    print("[OK] 补充信息窗口：ID 仅第三类可写（第一类忽略、第二类 403）")

    # ---- 3. 机器柜 / 辅料柜：仅 Shipment List 页手填 ----
    form = _row_form(entry, machine_cabinet="MSKU-PERM-1", aux_cabinet="AUX-PERM-1")
    r = c3.post(f"/api/shipments/{entry_id}/update", data=form)
    assert r.status_code == 200 and r.json()["ok"], r.text[:300]
    labels = {c["label"] for c in r.json()["changes"]}
    assert "机器柜" in labels and "辅料柜" in labels, labels
    db.expire_all()
    entry = db.get(ShipmentListEntry, entry_id)
    assert entry.machine_cabinet == "MSKU-PERM-1" and entry.aux_cabinet == "AUX-PERM-1"
    print("[OK] 行内编辑机器柜/辅料柜落库（改动留痕含 机器柜/辅料柜）")

    month_file = month_path(entry.month)
    assert month_file.exists(), month_file
    import openpyxl

    wb = openpyxl.load_workbook(month_file)
    ws = wb["Sheet1"]
    hit = False
    for row in range(2, ws.max_row + 1):
        if ws.cell(row, 23).value == "MSKU-PERM-1" and ws.cell(row, 24).value == "AUX-PERM-1":
            hit = True
            break
    wb.close()
    assert hit, "月度 Excel W/X 列应写入机器柜/辅料柜"
    print("[OK] 月度 Excel 写入 机器柜(W)/辅料柜(X)")

    page3 = c3.get("/shipments").text
    assert 'data-field="machine_cabinet"' in page3 and 'data-field="aux_cabinet"' in page3, \
        "Shipment List 页提供机器柜/辅料柜行内编辑"

    edit_page = admin.get(f"/invoices/edit?ids={inv_id}").text
    assert "machine_cabinet" not in edit_page and "机器柜" not in edit_page, \
        "编辑页弹窗不得提供机器柜/辅料柜"
    print("[OK] 编辑页弹窗无机器柜/辅料柜（只能在 Shipment List 页编辑）")

    r = admin.post("/api/invoices/export", params={"ids": str(inv_id)}, json={"form": FORM})
    assert r.status_code == 200 and r.json()["ok"], r.text[:500]
    db.expire_all()
    entry = db.get(ShipmentListEntry, entry_id)
    assert entry.machine_cabinet == "MSKU-PERM-1" and entry.aux_cabinet == "AUX-PERM-1", \
        "重新导出不得覆盖页面手填的机器柜/辅料柜"
    assert entry.ref_id == "T3-ID-003", "重新导出也不得抹掉 ID"
    print("[OK] 重新导出后机器柜/辅料柜/ID 保留页面手填值")


if __name__ == "__main__":
    main()
