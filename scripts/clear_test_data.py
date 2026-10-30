# -*- coding: utf-8 -*-
"""一次性清空测试数据。

清理范围（DB 已先备份到 data/backups/）：
1. 测试账号（保留 admin / type1 / type2 / type3 / chuanwu 五个真实账号）；
2. 测试装柜行：手工空行（is_manual）+ 指向已删除发票 [12]/[13] 的残留行；
3. 测试导出记录：TEST / T1 / 1234 / 464674545-CT / 26LF0846-CT 文件夹对应的
   ExportJob / ExportFileVersion，以及本次回归测试生成的 2609007-CT(TRUCK) 文件；
4. 测试账号产生的审计日志；
5. 真实装柜行里被测试污染的显眼字段（ref_id / vessel / package / etd / eta /
   container 等清空回原值）；
6. 对应测试文件（data/outputs + data/file_history）删除；
7. 重建 2026-09 / 2026-10 月度装柜 Excel。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import or_  # noqa: E402

from webapp.models import (  # noqa: E402
    AuditLog,
    ExportFileVersion,
    ExportJob,
    SessionLocal,
    ShipmentListEntry,
    User,
)
from webapp.service import rebuild_shipment_month  # noqa: E402

REAL_USERS = ("admin", "type1", "type2", "type3", "chuanwu")
TEST_FOLDERS = ("TEST", "T1", "1234", "464674545-CT", "26LF0846-CT")
MY_TEST_FILE = "2609007/VANCO INV#2609007-CT(TRUCK).xlsm"


def main() -> None:
    db = SessionLocal()

    # 1. 测试账号
    test_users = db.query(User).filter(User.username.notin_(REAL_USERS)).all()
    for u in test_users:
        db.delete(u)
    print(f"删除测试账号 {len(test_users)}: {[u.username for u in test_users]}")

    # 2. 测试装柜行
    manual_rows = (
        db.query(ShipmentListEntry).filter(ShipmentListEntry.is_manual.is_(True)).all()
    )
    orphan_rows = (
        db.query(ShipmentListEntry)
        .filter(ShipmentListEntry.invoice_ids.in_(["[12]", "[13]"]))
        .all()
    )
    for e in manual_rows + orphan_rows:
        db.delete(e)
    print(
        f"删除测试装柜行 {len(manual_rows) + len(orphan_rows)}: "
        f"手工 {[e.id for e in manual_rows]}, 孤儿 {[e.id for e in orphan_rows]}"
    )

    # 3. 测试导出记录
    del_versions = (
        db.query(ExportFileVersion)
        .filter(
            or_(
                ExportFileVersion.folder.in_(TEST_FOLDERS),
                (ExportFileVersion.folder == "2609007")
                & (
                    ExportFileVersion.filename
                    == "VANCO INV#2609007-CT(TRUCK).xlsm"
                ),
            )
        )
        .all()
    )
    for v in del_versions:
        db.delete(v)

    del_jobs = (
        db.query(ExportJob)
        .filter(
            or_(
                *[ExportJob.filename.like(f"{f}/%") for f in TEST_FOLDERS],
                ExportJob.filename == MY_TEST_FILE,
            )
        )
        .all()
    )
    for j in del_jobs:
        db.delete(j)
    print(
        f"删除测试导出记录: 版本 {len(del_versions)} 条, "
        f"任务 {len(del_jobs)} 条 ({[j.id for j in del_jobs]})"
    )

    # 4. 测试审计日志
    del_audit = db.query(AuditLog).filter(AuditLog.username.notin_(REAL_USERS)).delete(
        synchronize_session=False
    )
    print(f"删除测试审计日志 {del_audit} 条")

    # 5. 真实装柜行的显眼测试值清空
    e2 = db.get(ShipmentListEntry, 2)
    if e2 is not None:
        e2.date = "2026-09-10"
        e2.package = None
        e2.etd = ""
        e2.eta = ""
        e2.vessel = ""
        e2.ref_id = ""
        e2.container_1 = ""
        print(f"重置 id=2 ({e2.source_key}) 测试污染字段")

    e8 = db.get(ShipmentListEntry, 8)
    if e8 is not None and e8.container_1 == "TESTSAVE0001":
        e8.container_1 = ""
        print(f"重置 id=8 ({e8.source_key}) container_1")

    db.commit()

    # 6. 测试文件
    outputs = ROOT / "data" / "outputs"
    for f in TEST_FOLDERS:
        shutil.rmtree(outputs / f, ignore_errors=True)
    p = outputs / "2609007" / "VANCO INV#2609007-CT(TRUCK).xlsm"
    if p.exists():
        p.unlink()

    fh = ROOT / "data" / "file_history"
    shutil.rmtree(fh / "464674545-CT", ignore_errors=True)
    hist_dir = fh / "2609007"
    if hist_dir.exists():
        for f in hist_dir.glob("VANCO INV#2609007-CT(TRUCK).*.xlsm"):
            f.unlink(missing_ok=True)
    print("已删除测试文件（outputs + file_history）")

    # 7. 重建月度 Excel
    for month in ("2026-09", "2026-10"):
        try:
            rebuild_shipment_month(db, month)
            print(f"重建月度 Excel: {month}")
        except Exception as exc:  # noqa: BLE001
            print(f"重建 {month} 失败: {exc}")

    db.close()
    print("\n清空测试数据完成")


if __name__ == "__main__":
    main()
