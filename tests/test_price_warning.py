# -*- coding: utf-8 -*-
"""单元验证：单价单元格底色 + 查不到单价的提醒。"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

warnings.simplefilter("ignore")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.exporter import Exporter, ManualFields  # noqa: E402
from core.source_reader import read_source_file  # noqa: E402

SAMPLE = ROOT / "柬埔寨清关例子" / "VCO_202609_FB675_D202609007.xlsm"


def main() -> None:
    fails: list[str] = []

    def check(cond: bool, label: str) -> None:
        print(("[OK] " if cond else "[X] ") + label)
        if not cond:
            fails.append(label)

    exp = Exporter()

    # ---- 正常：面料单价能查到 → I 列黄色底纹、无提醒 ----
    src = read_source_file(SAMPLE)
    res = exp.build([src], ManualFields())
    ws = res.workbook["INV1"]
    i_cells = []
    for r in range(24, res.last_data_row + 1):
        if ws[f"I{r}"].value is not None:
            i_cells.append((r, ws[f"I{r}"]))
    check(len(i_cells) > 0, f"面料行 I 列有值（{len(i_cells)} 行）")
    fills = {c.fill.start_color.rgb[-6:] for _, c in i_cells}
    check("FFF2CC" in fills, f"查得到单价时 I 列为黄色底纹 {fills}")
    warn_msgs = [m for m in res.messages if "has no unit price" in m]
    check(not warn_msgs, "单价齐全时无提醒消息")
    res.workbook.close()

    # ---- 单价缺失 → I 列橙色底纹 + 提醒含单元格位置 ----
    src2 = read_source_file(SAMPLE)
    for it in src2.items:
        it.fabric_type = "NONEXIST_FABRIC_XYZ"
    res2 = exp.build([src2], ManualFields())
    ws2 = res2.workbook["INV1"]
    miss_fills = []
    for r in range(24, res2.last_data_row + 1):
        if ws2[f"D{r}"].value and str(ws2[f"D{r}"].value).strip().upper() == "NONEXIST_FABRIC_XYZ":
            miss_fills.append(ws2[f"I{r}"].fill.start_color.rgb[-6:])
    check(miss_fills and all(f == "F8CBAD" for f in miss_fills),
          f"查不到单价时 I 列为橙色底纹 {set(miss_fills)}")
    warn_msgs2 = [m for m in res2.messages if "has no unit price" in m]
    check(len(warn_msgs2) == 1, f"有提醒消息: {warn_msgs2}")
    check("I24" in warn_msgs2[0], f"提醒包含单元格位置: {warn_msgs2[0]}")
    res2.workbook.close()

    # ---- 快照 meta 返回 missing_prices ----
    sys.path.insert(0, str(ROOT))
    from fastapi.testclient import TestClient

    from webapp.main import app

    client = TestClient(app, base_url="http://testserver", follow_redirects=False)
    client.post("/login", data={"username": "admin", "password": "admin123", "next": ""},
                follow_redirects=False)
    r = client.get("/api/invoices/snapshot", params={"ids": "1"})
    d = r.json()
    check(d["ok"] and "missing_prices" in d["meta"],
          f"快照 meta 含 missing_prices: {d.get('meta', {}).get('missing_prices')}")

    print()
    if fails:
        print("FAILED:", len(fails))
        raise SystemExit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
