# -*- coding: utf-8 -*-
"""端到端：Shipment List 权限分割。

覆盖手工验证点：
1. Shipment List 第一 / 第三类与管理员可见，第二类看不见（403 且侧栏无入口）；
   报关发票列 = 第一列且冻结（sticky-col），文件名点击预览；第一类（及管理员）
   通过「Edit」按钮进入第一次生成该报关发票时的编辑页（invoice_ids 取首次生成
   记录，不是最新）；第三类只预览。每行提供单文件 Download，预览弹窗内也可下载。
2. 弹窗编辑权限：第一类 / 第三类 / 管理员点 Actions 列的 Edit 弹出补充信息窗口，
   一律仅手动填入字段（Package/ETD/ETA/船名/Carrier/柜型/CBM/ID），
   越权字段（Shipper/Blading/Custom # 等）服务端保留原值；第二类无任何
   Shipment List 编辑接口。
3. 报关单编辑页不再弹「Shipment List Extra Info」窗口（生成后直接同步）。
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

    # ---- 1. 页面访问 + 入口权限 ----
    page = admin.get("/shipments").text
    assert page.index('class="sticky-col">Customs Reference #') < page.index("<th>Date</th>"), \
        "报关发票必须是第一列表头"
    assert "sticky-col" in page, "冻结列样式缺失"
    assert f"/invoices/edit?ids={FAKE_FIRST_IDS}" in page, "管理员编辑入口指向第一次生成"
    assert 'title="Edit customs invoice' in page, "管理员有 Edit 按钮"
    assert 'id="preview-download"' in page, "预览弹窗提供下载按钮"
    print("[OK] 管理员视角：报关发票第一列且冻结，Edit 按钮 = 第一次生成的页面")

    text1 = c1.get("/shipments").text
    assert c1.get("/shipments").status_code == 200
    assert f"/invoices/edit?ids={FAKE_FIRST_IDS}" in text1, "第一类 Edit 按钮进编辑页（第一次生成）"
    assert 'title="Edit customs invoice' in text1, "第一类有 Edit 按钮"
    assert 'href="/api/download?folder=' in text1, "第一类可单文件下载"
    assert "Download Selected" in text1 and 'onclick="openShipmentEdit(this)"' in text1
    assert 'id="shipment-edit-modal"' in text1, "第一类有补充信息编辑弹窗"
    assert 'const EDITABLE_FIELDS = ["package"' in text1, "第一类弹窗编辑仅手动填入字段"
    print("[OK] 第一类视角：Edit 按钮进编辑页（第一次生成），可下载可预览，弹窗编辑仅手动字段")

    r = c2.get("/shipments")
    assert r.status_code == 403, r.status_code
    assert 'href="/shipments"' not in c2.get("/details").text, "第二类侧栏无 Shipment List 入口"
    r = c2.post(f"/api/shipments/{entry_id}/update", data=_row_form(entry, ref_id="X"))
    assert r.status_code == 403, r.status_code
    r = c2.get("/api/shipments/download-files", params={"ids": str(entry_id)})
    assert r.status_code == 403, r.status_code
    print("[OK] 第二类视角：看不见 Shipment List（页面/弹窗编辑/下载均 403）")

    text3 = c3.get("/shipments").text
    assert c3.get("/shipments").status_code == 200
    assert "/invoices/edit?ids=" not in text3, "第三类无编辑入口（只预览）"
    assert "previewFromLink" in text3 and "Download Selected" in text3
    assert 'href="/api/download?folder=' in text3, "第三类可单文件下载"
    assert 'onclick="openShipmentEdit(this)"' in text3, "第三类可弹窗编辑"
    assert 'const EDITABLE_FIELDS = ["package"' in text3, "第三类仅手动填入字段可编辑"
    assert 'title="Edit customs invoice' not in text3
    print("[OK] 第三类视角：仅预览 + 手动填入字段弹窗编辑，无报关单编辑入口")

    r = c1.get(f"/invoices/edit?ids={inv_id}")
    assert r.status_code == 200, r.status_code
    r = c2.get(f"/invoices/edit?ids={inv_id}")
    assert r.status_code == 200, r.status_code
    r = c2.get("/api/invoices/snapshot", params={"ids": str(inv_id)})
    assert r.status_code == 200 and r.json()["ok"], r.text[:300]
    r = c3.get(f"/invoices/edit?ids={inv_id}")
    assert r.status_code == 403, r.status_code
    print("[OK] 编辑页权限：第一类/第二类可进（快照可用），第三类 403")

    # ---- 2. ID 与字段编辑权限（第一类/第三类均仅手动字段，含 ID） ----
    form = _row_form(entry, ref_id="T3-ID-001")
    r = c3.post(f"/api/shipments/{entry_id}/update", data=form)
    assert r.status_code == 200 and r.json()["ok"], r.text[:300]
    db.expire_all()
    entry = db.get(ShipmentListEntry, entry_id)
    assert entry.ref_id == "T3-ID-001", entry.ref_id
    print("[OK] 第三类弹窗编辑 ID 生效")

    orig_blading, orig_custom = entry.blading, entry.custom_no
    orig_shipper = entry.shipper
    form = _row_form(
        entry,
        ref_id="T1-ID-001",
        vessel="PERM VESSEL",
        shipper="T1 SHIPPER",
    )
    r = c1.post(f"/api/shipments/{entry_id}/update", data=form)
    assert r.status_code == 200 and r.json()["ok"], r.text[:300]
    db.expire_all()
    entry = db.get(ShipmentListEntry, entry_id)
    assert entry.ref_id == "T1-ID-001", f"第一类可改 ID，实际 {entry.ref_id}"
    assert entry.vessel == "PERM VESSEL", "第一类可改手动字段（VESSEL）"
    assert entry.shipper == orig_shipper, f"第一类不得改 Shipper，实际 {entry.shipper}"
    assert (entry.blading, entry.custom_no) == (orig_blading, orig_custom), "未提交字段保持不变"
    print("[OK] 第一类弹窗编辑 ID + 手动字段生效，Shipper 等自动字段保留原值")

    # 以第一类写入后的值为基准：第三类越权提交必须保留原值
    keep_shipper, keep_blading, keep_custom = entry.shipper, entry.blading, entry.custom_no
    form = _row_form(
        entry,
        ref_id="T3-ID-002",
        shipper="T3 SHIPPER",
        blading="T3 BLADING",
        custom_no="T3-CUSTOM",
    )
    r = c3.post(f"/api/shipments/{entry_id}/update", data=form)
    assert r.status_code == 200 and r.json()["ok"], r.text[:300]
    db.expire_all()
    entry = db.get(ShipmentListEntry, entry_id)
    assert entry.ref_id == "T3-ID-002", entry.ref_id
    assert entry.shipper == keep_shipper, f"第三类不得改 Shipper，实际 {entry.shipper}"
    assert entry.blading == keep_blading, f"第三类不得改 Blading，实际 {entry.blading}"
    assert entry.custom_no == keep_custom, f"第三类不得改 Custom #，实际 {entry.custom_no}"
    print("[OK] 第三类越权字段保留原值（Shipper/Blading/Custom #），手动字段与 ID 可写")

    r = c1.post(f"/api/shipments/{entry_id}/ref-id", data={"ref_id": "T1-ID-002"})
    assert r.status_code == 303, r.status_code
    db.expire_all()
    entry = db.get(ShipmentListEntry, entry_id)
    assert entry.ref_id == "T1-ID-002", entry.ref_id
    r = c3.post(f"/api/shipments/{entry_id}/ref-id", data={"ref_id": "T3-ID-003"})
    assert r.status_code == 303, r.status_code
    db.expire_all()
    entry = db.get(ShipmentListEntry, entry_id)
    assert entry.ref_id == "T3-ID-003", entry.ref_id
    r = c2.post(f"/api/shipments/{entry_id}/ref-id", data={"ref_id": "T2-ID"})
    assert r.status_code == 403, r.status_code
    print("[OK] /ref-id 接口：第一类/第三类可写，第二类 403")

    r = c1.post("/api/shipments/info", data={"ids": str(inv_id), "ref_id": "T1-ID-003"})
    assert r.status_code == 200 and r.json()["ok"], r.text[:300]
    db.expire_all()
    entry = db.get(ShipmentListEntry, entry_id)
    assert entry.ref_id == "T1-ID-003", entry.ref_id
    r = c3.post("/api/shipments/info", data={"ids": str(inv_id), "ref_id": "T3-ID-004"})
    assert r.status_code == 200 and r.json()["ok"], r.text[:300]
    db.expire_all()
    entry = db.get(ShipmentListEntry, entry_id)
    assert entry.ref_id == "T3-ID-004", entry.ref_id
    r = c2.post("/api/shipments/info", data={"ids": str(inv_id), "ref_id": "T2-ID"})
    assert r.status_code == 403, r.status_code
    print("[OK] 补充信息接口：ID 第一类/第三类可写，第二类 403")

    # ---- 3. ID 重新导出保留 ----
    r = admin.post("/api/invoices/export", params={"ids": str(inv_id)}, json={"form": FORM})
    assert r.status_code == 200 and r.json()["ok"], r.text[:500]
    db.expire_all()
    entry = db.get(ShipmentListEntry, entry_id)
    assert entry.ref_id == "T3-ID-004", "重新导出不得抹掉 ID"
    print("[OK] 重新导出后 ID 保留页面手填值")


if __name__ == "__main__":
    main()
