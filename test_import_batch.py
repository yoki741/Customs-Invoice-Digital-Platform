# -*- coding: utf-8 -*-
"""批量导入测试：把 examples 目录下的样例全部导入并验证导出。"""

from __future__ import annotations

import sys
import warnings
from datetime import date
from pathlib import Path

warnings.simplefilter("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import DATA_DIR, ensure_dirs  # noqa: E402
from webapp.importer import import_source_file  # noqa: E402
from webapp.models import Invoice, SessionLocal, User, init_db  # noqa: E402
from webapp.service import export_invoices  # noqa: E402

FILES = [
    r"D:\TotalProgram\Invoice汇总\柬埔寨清关例子\海运例子 清关资料 货代提单\DKT_202608_FB338_26LF0845.xlsm",
    r"D:\TotalProgram\Invoice汇总\柬埔寨清关例子\海运例子 清关资料 货代提单\DKT_202609_FB338_26LF0846.xlsm",
    r"D:\TotalProgram\Invoice汇总\柬埔寨清关例子\海运例子 清关资料 货代提单\VCO_202608_FB338_26LF0845-1.xlsm",
    r"D:\TotalProgram\Invoice汇总\柬埔寨清关例子\海运例子 清关资料 货代提单\VCO_202609_FB338_26LF0846-1.xlsm",
    r"D:\TotalProgram\Invoice汇总\柬埔寨清关例子\海运例子 清关资料2\DKT_202608_FB982_YS2026081502.xlsm",
    r"D:\TotalProgram\Invoice汇总\柬埔寨清关例子\空运例子 清关资料\DKT_202609_FB932_2609007.xlsm",
    r"D:\TotalProgram\Invoice汇总\柬埔寨清关例子\空运例子 清关资料\VCO_202609_FB932_2609006.xlsm",
    r"D:\TotalProgram\Invoice汇总\柬埔寨清关例子\陆运例子 清关资料\VCO_202609_FB429_LY260905-1DM.xlsm",
    r"D:\TotalProgram\Invoice汇总\柬埔寨清关例子\VCO_202609_FB675_D202609007.xlsm",
]


def _supplier(delivery_to: str, bill_to: str) -> str:
    text = f"{delivery_to or ''} {bill_to or ''}"
    if "VANCO" in text.upper():
        return "VANCO"
    if "EASTEX" in text.upper() or "DAKOTA" in text.upper():
        return "EASTEX"
    return ""


def main() -> None:
    ensure_dirs()
    db_path = DATA_DIR / "app.db"
    if db_path.exists():
        db_path.unlink()
    init_db()

    db = SessionLocal()
    user = User(username="tester", password_hash="x", role="admin")
    db.add(user)
    db.commit()

    ok = fail = 0
    for raw in FILES:
        path = Path(raw)
        print("=" * 70)
        print(f"文件: {path.name}")
        if not path.exists():
            print("  [FAIL] 文件不存在")
            fail += 1
            continue
        try:
            inv, created = import_source_file(db, path, user)
        except Exception as exc:
            print(f"  [FAIL] 导入失败: {exc}")
            fail += 1
            continue
        tag = "新建" if created else "替换"
        print(f"  [OK] 导入 ({tag})")
        print(f"       Invoice No.  : {inv.invoice_no}")
        print(f"       运输方式      : {inv.transport}")
        print(f"       公司          : {inv.company}")
        print(f"       Delivery to   : {inv.delivery_to}")
        print(f"       明细条数      : {inv.item_count}")
        print(f"       净重/毛重     : {inv.net_weight} / {inv.gross_weight}")
        print(f"       总金额        : {inv.total_amount}")
        print(f"       发票日期      : {inv.invoice_date}")

        form = {
            "date": date.today().isoformat(),
            "bill_no": inv.invoice_no or "",
            "ship_mode": inv.ship_mode or "",
            "supplier": _supplier(inv.delivery_to, inv.bill_to),
            "flight_no": inv.vessel_flight or "",
        }
        try:
            out_path, info = export_invoices(db, [inv], form, user)
        except Exception as exc:
            print(f"  [FAIL] 导出失败: {exc}")
            fail += 1
            continue
        size = out_path.stat().st_size
        print(f"  [OK] 导出 {info['filename']} ({size} bytes)")
        print(f"       海关重量      : {info['customs_weight']}")
        print(f"       总金额        : {info['total_amount']}")
        ok += 1

    db.close()
    print("=" * 70)
    print(f"结果: 成功 {ok} / 失败 {fail} / 共 {len(FILES)}")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
