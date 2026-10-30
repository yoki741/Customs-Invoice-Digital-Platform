# -*- coding: utf-8 -*-
"""全量清空：发票 + Shipment List + 导出记录 + 审计日志 + 生成文件。

保留：users 五个账号（admin/type1/type2/type3/chuanwu）。
删除：invoices / invoice_items / shipment_list_entries / export_jobs /
export_file_versions / audit_logs，以及 data/outputs、data/file_history 下生成文件。
不动：柬埔寨清关例子等源文件、data/backups 备份。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from webapp.models import (  # noqa: E402
    AuditLog,
    ExportFileVersion,
    ExportJob,
    Invoice,
    InvoiceItem,
    SessionLocal,
    ShipmentListEntry,
)


def main() -> None:
    db = SessionLocal()
    n = {}
    n["invoice_items"] = db.query(InvoiceItem).delete(synchronize_session=False)
    n["shipment_entries"] = db.query(ShipmentListEntry).delete(synchronize_session=False)
    n["file_versions"] = db.query(ExportFileVersion).delete(synchronize_session=False)
    n["jobs"] = db.query(ExportJob).delete(synchronize_session=False)
    n["invoices"] = db.query(Invoice).delete(synchronize_session=False)
    n["audit_logs"] = db.query(AuditLog).delete(synchronize_session=False)
    db.commit()
    db.close()
    for k, v in n.items():
        print(f"{k}: 删除 {v} 条")

    for d in ("data/outputs", "data/file_history"):
        p = ROOT / d
        if p.exists():
            for child in p.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
    print("data/outputs + data/file_history 已清空")


if __name__ == "__main__":
    main()
