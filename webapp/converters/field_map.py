# -*- coding: utf-8 -*-
"""人工填写字段定义（编辑页高亮 + 提交白名单）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class EditableField:
    """一个需要人工填写的字段。"""

    key: str
    label: str
    sheet: str
    cell: str
    hint: str = ""
    group: str = "Basic Info"
    multiline: bool = False
    required: bool = False


# 注意：顺序即前端展示顺序
EDITABLE_FIELDS: list[EditableField] = [
    EditableField("date", "Date", "INV1", "J9",
                  "Customs invoice date, format YYYY-MM-DD", required=True),
    EditableField("bill_no", "Invoice No.", "INV1", "J10",
                  "Also used for the output filename and the REF line", required=True),
    EditableField("ship_mode", "Ship Mode", "INV1", "B17",
                  "e.g. by Sea / by AIR / by TRUCK", required=True),
    EditableField("place_receipt", "Place of Receipt / Port of Loading", "INV1", "A18",
                  "Sea: Port of loading, Air: Airport of departure, "
                  "Truck: Place of initial receipt. Match the cell display in the sheet "
                  "(keep the label text before the colon)"),
    EditableField("port_loading", "Port of Loading (Truck)", "INV1", "A19",
                  "Truck only. Match the cell display in the sheet "
                  "(keep the label text before the colon)"),
    EditableField("container_info", "Container / Seal No.", "INV1", None,
                  "CONTAINER NO / SEAL NO area, one per row, mapping to Container 1-4 "
                  "(up to 4 rows)",
                  multiline=True),
    EditableField("flight_no", "Flight No.", "PKL1", "I17",
                  "Air only, defaults to the source file Vessel/Flight/Truck Info"),
]

FIELD_BY_KEY: dict[str, EditableField] = {f.key: f for f in EDITABLE_FIELDS}

# 不落在单元格上的补充字段
EXTRA_FIELDS: list[EditableField] = [
    EditableField("supplier", "Supplier", "", "",
                  "Determines the output filename prefix (VANCO / EASTEX ...)",
                  group="Output Info",
                  required=True),
    EditableField("unit_prices", "Unit Prices (manually editable)", "", "",
                  "Read from the price table automatically by default; "
                  "can be overridden manually per fabric type",
                  group="Auto-calculated", multiline=False, required=False),
]

ALLOWED_KEYS: set[str] = {f.key for f in EDITABLE_FIELDS} | {f.key for f in EXTRA_FIELDS}


def editable_cells(
    transport: str,
    container_row: Optional[int],
    *,
    include_static: bool = True,
) -> dict[str, list[str]]:
    """按运输方式计算需要高亮/可编辑的单元格。

    Args:
        transport: 海运 / 空运 / 陆运。
        container_row: INV1 中 CONTAINER 行号（空运无该区域）。
        include_static: 是否包含与运输方式无关的固定字段。

    Returns:
        {工作表名: [单元格坐标]}，坐标为大写。
    """
    cells: dict[str, list[str]] = {"INV1": [], "PKL1": []}

    if include_static:
        cells["INV1"].extend(["J9", "J10", "B17", "A18"])

    if transport == "陆运":
        cells["INV1"].append("A19")

    if container_row:
        for offset in range(4):
            cells["INV1"].append(f"A{container_row + 1 + offset}")

    if transport == "空运":
        cells["PKL1"].append("I17")

    return {sheet: sorted(set(coords), key=lambda x: int("".join(filter(str.isdigit, x)) or 0))
            for sheet, coords in cells.items() if coords}


def validate_form(raw: Optional[dict[str, Any]]) -> tuple[dict[str, str], list[str]]:
    """清洗前端提交的字段，返回 (字段值, 错误列表)。"""
    raw = raw or {}
    values: dict[str, str] = {}
    errors: list[str] = []

    for key in ALLOWED_KEYS:
        value = raw.get(key)
        values[key] = "" if value is None else str(value).strip()

    for field in EDITABLE_FIELDS + EXTRA_FIELDS:
        if field.required and not values.get(field.key):
            errors.append(f"{field.label} is required")

    return values, errors
