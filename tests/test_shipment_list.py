# -*- coding: utf-8 -*-
"""装柜列表（Shipment List）映射、ETA 与模板写入测试。"""

from __future__ import annotations

import sys
import tempfile
import warnings
from datetime import date
from pathlib import Path

warnings.simplefilter("ignore")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import openpyxl  # noqa: E402

from core.shipment_list import (  # noqa: E402
    build_row,
    build_row_from_result,
    eta_for,
    month_of,
    mode_from,
    parse_date,
    write_workbook,
)
from core.source_reader import DetailItem, SourceData  # noqa: E402


def _source(**kwargs) -> SourceData:
    defaults = dict(
        file_path=Path("sample.xlsm"),
        invoice_no="SHSJ2610051A",
        invoice_date="2026-10-21",
        supplier_code="LEFEI",
        delivery_to="VANCO INDUSTRIAL CO., LTD",
        ship_mode="By Sea",
        vessel_flight="SITC RUNDE V.2622S",
        container_number="FBLU0186990",
        origin_place="NINGBO, CHINA",
        customs_agent="SITC",
        rolls=307,
        net_weight=6664,
        gross_weight=6900,
    )
    po_no = kwargs.pop("po_no", "26LF1025-1")
    defaults.update(kwargs)
    data = SourceData(**defaults)
    data.items = [
        DetailItem(
            row=17,
            po_no=po_no,
            description="KNITTED FABRIC",
            fabric_type="JC",
            carton=10,
            net_weight=100,
        )
    ]
    return data


def _result_workbook() -> openpyxl.Workbook:
    """构造一个最小清关文件工作簿（INV1/PKL1）。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "INV1"
    ws["A1"] = "ZHEJIANG LEFEI TEXTILE CO.,LTD."
    ws["B9"] = "VANCO INDUSTRIAL CO., LTD."
    ws["J9"] = "2026-10-22"
    ws["J10"] = "SHSJ2610051A"
    ws["B17"] = "By Sea"
    ws["A18"] = "Port of loading: NINGBO, CHINA"
    ws["A36"] = "REF:26LF0845-1"
    ws["D24"] = "JC"
    ws["G24"] = 6000
    ws["D25"] = "JC"
    ws["G25"] = 664
    ws["D26"] = "POLY"
    ws["G26"] = 123
    ws["A38"] = "CONTAINER NO / SEAL NO:"
    ws["A39"] = "FBLU0186990 / SEAL123"
    ws["A40"] = "TEMU9126356 / SEAL456"
    pkl = wb.create_sheet("PKL1")
    pkl["K24"] = 6900
    return wb


def main() -> None:
    fails: list[str] = []

    def check(cond: bool, label: str) -> None:
        print(("[OK] " if cond else "[X] ") + label)
        if not cond:
            fails.append(label)

    check(parse_date("2026-10-22") == date(2026, 10, 22), "解析 YYYY-MM-DD")
    check(parse_date("2026/10/22") == date(2026, 10, 22), "解析 YYYY/MM/DD")
    check(parse_date("2026-10-22 00:00:00") == date(2026, 10, 22), "解析带时间")
    check(parse_date("") is None, "空日期")

    check(mode_from("空运") == "AIR", "运输方式 空运→AIR")
    check(mode_from("海运") == "SEA", "运输方式 海运→SEA")
    check(mode_from("", "by Truck") == "TRUCK", "运输方式 Truck→TRUCK")

    etd = date(2026, 10, 20)
    check(eta_for("AIR", etd) == date(2026, 10, 21), "ETA 空运 +1")
    check(eta_for("SEA", etd) == date(2026, 10, 28), "ETA 海运 +8")
    check(eta_for("TRUCK", etd) == date(2026, 10, 28), "ETA 汽运 +8")

    row = build_row(
        [_source()],
        {
            "date": "2026-10-22",
            "bill_no": "SHSJ2610051A",
            "place_receipt": "Port of loading: Ningbo, China",
            "container_info": "FBLU0186990 / SEAL123",
        },
        company="Vanco",
        transport="海运",
    )
    check(row.custom_no == "SHSJ2610051A_CT", "Vanco 发票号后缀 _CT")
    check(row.blading == "SHSJ2610051A", "提单号取 bill_no")
    check(row.fty_inv == "26LF1025-1", "Fty Inv 取 PO No.")
    check(row.shipper == "LEFEI", "Shipper 取供应商代码")
    check(row.est_vco == "Vanco", "EST/VCO 取我司工厂")
    check(row.package == 307, "Package 取件数")
    check(row.containers == ["FBLU0186990"], "柜号自动提取")
    check(row.pol == "Ningbo, China", "POL 去掉标签前缀")
    check(row.etd == date(2026, 10, 21), "ETD 取发票日期")
    check(row.eta == date(2026, 10, 29), "ETA = ETD + 8")
    check(row.vessel == "SITC RUNDE V.2622S", "VESSEL 预填航次")
    check(row.carrier == "SITC", "Carrier 预填货代")
    check(month_of(row.date) == "2026-10", "月份归属")

    row_eastex = build_row(
        [_source(delivery_to="EASTEX INDUSTRIAL CO., LTD", supplier_code="SHUIXIN")],
        {"bill_no": "XMNCT2610214A"},
        company="Eastex",
        transport="陆运",
    )
    check(row_eastex.custom_no == "XMNCT2610214A_BT", "Eastex 发票号后缀 _BT")
    check(row_eastex.mode == "TRUCK", "陆运 Mode=TRUCK")

    merged = build_row(
        [
            _source(po_no="26LF1025-1", rolls=100, net_weight=1000, gross_weight=1100,
                    container_number="TEMU9126356"),
            _source(po_no="26LF1026", rolls=50, net_weight=500, gross_weight=550,
                    container_number="FBLU0186990"),
        ],
        {"bill_no": "2609007"},
        company="Vanco",
        transport="海运",
    )
    check(merged.fty_inv == "26LF1025-1, 26LF1026", "合并导出的 Fty Inv 用逗号连接")
    check(merged.package == 150, "合并件数求和")
    check(merged.nw_kg == 1500 and merged.gw_kg == 1650, "合并重量求和")
    check(merged.containers == ["TEMU9126356", "FBLU0186990"], "多柜号写入 1-2")
    check(merged.custom_no == "2609007_CT", "合并导出单号")

    res_wb = _result_workbook()
    try:
        res_row = build_row_from_result(
            res_wb, company="Vanco", transport="海运", customs_weight=6664
        )
        check(res_row.shipper == "ZHEJIANG LEFEI TEXTILE CO.,LTD.", "清关文件 Shipper 取 INV1!A1")
        check(res_row.est_vco == "Vanco", "清关文件 EST/VCO 取工厂")
        check(res_row.custom_no == "SHSJ2610051A_CT", "清关文件 Custom # 后缀")
        check(res_row.blading == "SHSJ2610051A", "清关文件 Blading 取发票号")
        check(res_row.fty_inv == "26LF0845-1", "清关文件 Fty Inv 取 REF")
        check(res_row.pol == "NINGBO, CHINA", "清关文件 POL 取 A18")
        check(res_row.containers == ["FBLU0186990", "TEMU9126356"], "清关文件柜号")
        check(res_row.nw_kg == 6664, "清关文件净重=报关净重")
        check(res_row.gw_kg == 6900, "清关文件毛重取 PKL1!K24")
        check(res_row.description == "JC / POLY", "清关文件分类去重")
        check(res_row.mode == "SEA", "清关文件运输方式")
        check(res_row.package is None, "Package 留空待人工")
        check(res_row.etd is None and res_row.eta is None, "ETD/ETA 留空")
        check(res_row.vessel == "" and res_row.carrier == "", "VESSEL/Carrier 留空")
    finally:
        res_wb.close()

    out = Path(tempfile.gettempdir()) / "shipment_list_test.xlsx"
    write_workbook([row, row_eastex], out)
    wb = openpyxl.load_workbook(out)
    try:
        ws = wb["Sheet1"]
        check(ws["A1"].value == "Date", "模板表头保留")
        check(ws["A2"].value.date() == date(2026, 10, 22), "第 2 行写入 Date")
        check(ws["E2"].value == "SHSJ2610051A_CT", "第 2 行写入 Custom #")
        check(ws["B3"].value == "26LF1025-1", "第 3 行写入 Fty Inv")
        check(ws["A6"].value is None, "模板说明行已清空，避免与数据冲突")
        check("mmm" in ws["A2"].number_format, "日期格式沿用模板")
        check(ws["A2"].border.left.style == "thin", "边框沿用模板")
    finally:
        wb.close()
    out.unlink(missing_ok=True)

    if fails:
        print(f"\n失败 {len(fails)} 项")
        raise SystemExit(1)
    print("\n全部通过")


if __name__ == "__main__":
    main()
