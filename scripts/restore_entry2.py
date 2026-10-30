# -*- coding: utf-8 -*-
"""把 id=2（Eastex:26LF0845 的测试污染残留）恢复到真实装柜数据。

真实数据取自 data/backups/app_20261029_082020.db 里的 id=1（该真实行后来被误删，
只剩 id=2 这个被测试污染的重复行）。字段值：
SEA / SHANGHAI, CHINA / 26LF0845 / 26LF0845_BT / TEMU1234567 / TRHU5734520 等。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from webapp.models import SessionLocal, ShipmentListEntry  # noqa: E402
from webapp.service import rebuild_shipment_month  # noqa: E402

REAL = {
    "job_id": 115,
    "month": "2026-08",
    "date": "2026-08-31",
    "fty_inv": "26LF0845",
    "shipper": "ZHEJIANG LEFEI TEXTILE CO.,LTD.",
    "est_vco": "Eastex",
    "custom_no": "26LF0845_BT",
    "package": 435.0,
    "blading": "26LF0845",
    "container_1": "TEMU1234567",
    "container_2": "TRHU5734520",
    "container_3": "",
    "container_4": "",
    "nw_kg": 10158.0,
    "gw_kg": 11024.0,
    "description": "KNITTED FABRIC",
    "mode": "SEA",
    "etd": "2026-08-31",
    "eta": "2026-09-08",
    "pol": "SHANGHAI, CHINA",
    "vessel": "SITC KEELUNG/2620S",
    "carrier": "SDAM",
    "ref_id": "",
    "container_type": "",
    "cbm": None,
}


def main() -> None:
    db = SessionLocal()
    e = db.get(ShipmentListEntry, 2)
    assert e is not None and e.source_key == "Eastex:26LF0845", e
    print("before:", e.mode, e.pol, e.blading, e.custom_no, e.container_1, e.container_2)
    for k, v in REAL.items():
        setattr(e, k, v)
    db.commit()
    print("after :", e.mode, e.pol, e.blading, e.custom_no, e.container_1, e.container_2)

    for month in ("2026-08", "2026-09", "2026-10"):
        try:
            rebuild_shipment_month(db, month)
            print(f"重建月度 Excel: {month}")
        except Exception as exc:  # noqa: BLE001
            print(f"重建 {month} 失败: {exc}")

    db.close()
    print("id=2 已恢复为真实数据")


if __name__ == "__main__":
    main()
