# -*- coding: utf-8 -*-
"""端到端：版本化导出 + 隐藏追踪表 + 回传归档 + 并发保存提醒。"""

from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

warnings.simplefilter("ignore")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from webapp.main import app  # noqa: E402
from core.exporter import read_trace_sheet  # noqa: E402

SAMPLE = ROOT / "柬埔寨清关例子" / "VCO_202609_FB675_D202609007.xlsm"


def _csrf_from(html: str) -> str:
    import re

    m = re.search(r'name="csrf"\s+value="([^"]+)"', html)
    assert m, "未找到 CSRF token"
    return m.group(1)


def main() -> None:
    # 清理历史测试数据，保证可重复运行
    from webapp.models import ExportFileVersion as _EV, SessionLocal as _SL

    db = _SL()
    db.query(_EV).delete()
    db.commit()
    db.close()
    for p in (
        ROOT / "data" / "file_history",
        ROOT / "data" / "outputs" / "2609007",
    ):
        import shutil

        shutil.rmtree(p, ignore_errors=True)

    client = TestClient(app, base_url="http://testserver", follow_redirects=False)
    client.post(
        "/login",
        data={"username": "admin", "password": "admin123", "next": ""},
        follow_redirects=False,
    )

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

    # ---- 1. 连续生成两次：平台唯一文件，命名规则不变，旧文件时间戳留底 ----
    r1 = client.post("/api/invoices/export", params={"ids": "1"}, json=payload)
    assert r1.status_code == 200 and r1.json()["ok"], r1.text
    d1 = r1.json()
    v1 = d1["version"]
    assert d1["filename"] == "VANCO INV#2609007-BT(SEA).xlsm", d1["filename"]
    print("[OK] 第 1 次生成:", d1["filename"], "（内部序号", str(v1), "）")

    r2 = client.post("/api/invoices/export", params={"ids": "1"}, json=payload)
    assert r2.status_code == 200 and r2.json()["ok"], r2.text
    d2 = r2.json()
    assert d2["version"] == v1 + 1, (d1["version"], d2["version"])
    assert d2["filename"] == d1["filename"], "正式文件名必须保持固定命名规则"
    assert d2["path"] == d1["path"], "平台文件唯一，两次生成同一路径"
    assert Path(d2["path"]).exists(), "正式文件存在"
    out_dir = Path(d2["path"]).parent
    matches = [f.name for f in out_dir.glob("VANCO INV#2609007-BT(SEA).xlsm")]
    assert matches == ["VANCO INV#2609007-BT(SEA).xlsm"], f"该文件在平台上唯一: {matches}"
    assert "_v" not in d2["filename"] and "_v" not in d2["folder"], "命名中不允许出现 _vN"
    hist_dir = out_dir.parent.parent / "file_history" / "2609007"
    hist_files = list(hist_dir.glob("VANCO INV#2609007-BT(SEA).*.xlsm"))
    assert len(hist_files) >= 1, "旧文件已按时间戳留底"
    assert all("_v" not in f.name for f in hist_files), "留底文件名也是时间戳命名"
    print("[OK] 第 2 次生成：文件唯一、命名不变、旧文件时间戳留底:", [f.name for f in hist_files])

    # 数据库记录：两条 export 记录，文件名一致，旧记录带 backup_path
    from webapp.models import ExportFileVersion as _EV, SessionLocal as _SL

    db = _SL()
    recs = db.query(_EV).filter(_EV.kind == "export").order_by(_EV.id).all()
    db.close()
    assert len(recs) == 2, [r.filename for r in recs]
    assert all(r.filename == "VANCO INV#2609007-BT(SEA).xlsm" for r in recs)
    assert Path(recs[-1].backup_path).exists(), "最新一次生成的旧文件留底存在"
    print("[OK] 修改记录正确（两次生成、唯一文件名、留底路径有效）")

    # ---- 2. 隐藏追踪表 ----
    trace = read_trace_sheet(Path(d2["path"]))
    assert trace.get("EXPORTED_BY") == "admin", trace
    assert trace.get("VERSION") == str(d2["version"]), trace
    assert trace.get("JOB_ID"), trace
    assert trace.get("INVOICE_IDS") == "1", trace
    assert trace.get("INVOICE_NOS"), trace
    print("[OK] 追踪表已写入:", trace)

    # 追踪表在 Excel 层面必须是 veryHidden
    import openpyxl

    wb = openpyxl.load_workbook(d2["path"], keep_vba=False)
    assert "_PLATFORM_TRACE" in wb.sheetnames
    assert wb["_PLATFORM_TRACE"].sheet_state == "veryHidden", wb["_PLATFORM_TRACE"].sheet_state
    wb.close()
    print("[OK] 追踪表状态为 veryHidden（Excel 界面不可见/不可操作）")

    # ---- 3. 报关发票页：唯一文件 + 修改记录 ----
    r = client.get("/files")
    assert r.status_code == 200 and "报关发票" in r.text
    assert d2["filename"] in r.text, "页面列出唯一文件"
    assert "_v" not in r.text.split("<tbody")[1].split("</tbody>")[0], "页面无 _vN 文件名"
    assert "修改记录" in r.text and "生成次数" in r.text
    r = client.get("/files", params={"q": "VANCO INV#2609007"})
    assert r.status_code == 200
    print("[OK] 报关发票页：唯一文件、无 _vN、有修改记录入口")

    # ---- 4. 修改记录时间线（含编辑保存）----
    client.post(
        "/api/invoices/manual-save",
        params={"ids": "1"},
        json={"form": {"bill_no": "2609007", "supplier": "VANCO"},
              "last_saved_at": d2["saved_at"]},
    )
    r = client.get(
        "/api/files/history",
        params={"folder": d2["folder"], "name": d2["filename"]},
    )
    h = r.json()
    assert h["ok"], h
    kinds = [row["kind"] for row in h["rows"]]
    assert kinds.count("export") == 2 and kinds.count("save") >= 1, kinds
    assert any(row["backup"] for row in h["rows"]), "有留底下载链接"
    bk = next(row["backup"] for row in h["rows"] if row["backup"])
    r = client.get(bk)
    assert r.status_code == 200 and len(r.content) > 1000, "留底文件可下载"
    print("[OK] 修改记录时间线（生成2次+保存N次），留底可下载")

    # ---- 5. 上传接口已移除 ----
    r = client.post(
        "/api/files/upload",
        files=[("file", ("edited.xlsm", b"x", "application/octet-stream"))],
    )
    assert r.status_code in (404, 405), r.status_code
    print("[OK] /api/files/upload 接口已移除")

    # ---- 7. 并发保存冲突 ----
    r = client.get("/api/invoices/snapshot", params={"ids": "1"})
    meta = r.json()["meta"]
    base_ts = meta.get("last_saved_at") or "2026-09-10 10:00:00"

    r = client.post(
        "/api/invoices/manual-save",
        params={"ids": "1"},
        json={"form": {"bill_no": "2609007"}, "last_saved_at": base_ts},
    )
    d = r.json()
    assert d["ok"], d
    assert not d.get("conflict"), d
    print("[OK] 基于最新版本保存无冲突")

    r = client.post(
        "/api/invoices/manual-save",
        params={"ids": "1"},
        json={"form": {"bill_no": "2609007"}, "last_saved_at": "2026-09-10 10:00:00"},
    )
    d = r.json()
    assert d["ok"] and d.get("conflict"), d
    assert d.get("saved_by") == "admin", d
    print("[OK] 基于旧版本保存 → 检测到冲突，提示保存人:", d.get("saved_by"))

    r = client.post(
        "/api/invoices/manual-save",
        params={"ids": "1"},
        json={"form": {"bill_no": "2609007"}, "last_saved_at": "2026-09-10 10:00:00", "force": True},
    )
    d = r.json()
    assert d["ok"] and not d.get("conflict"), d
    print("[OK] force 覆盖保存成功")

    # ---- 8. 快照返回最近保存人 ----
    r = client.get("/api/invoices/snapshot", params={"ids": "1"})
    d = r.json()
    assert d["ok"] and d["meta"].get("last_saved_by") == "admin", d.get("meta")
    assert d["meta"].get("last_saved_at"), d.get("meta")
    print("[OK] 快照返回最近保存人/时间:", d["meta"]["last_saved_by"], d["meta"]["last_saved_at"])

    print("\n全部版本化/追踪/回传/并发测试通过")


if __name__ == "__main__":
    main()
