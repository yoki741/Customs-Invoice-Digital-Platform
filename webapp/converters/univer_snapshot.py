# -*- coding: utf-8 -*-
"""openpyxl Workbook → Univer IWorkbookData 快照。

只做「读取模板 + 预填结果」到 Univer 网格的单向转换，样式覆盖：
字体、字号、粗斜体、下划线、删除线、前景/背景色、边框、对齐、换行、数字格式、
合并单元格、列宽、行高。公式按原样保留，由 Univer 公式引擎在前端计算。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time
from typing import Any, Optional

import openpyxl
from openpyxl.cell.cell import MergedCell
from openpyxl.workbook.workbook import Workbook

# ---- Univer 枚举值（与 @univerjs/core 保持一致） ----
CV_STRING = 1
CV_NUMBER = 2
CV_BOOLEAN = 3

H_UNSPEC, H_LEFT, H_CENTER, H_RIGHT, H_JUST = 0, 1, 2, 3, 4
V_UNSPEC, V_TOP, V_MIDDLE, V_BOTTOM = 0, 1, 2, 3
W_UNSPEC, W_OVERFLOW, W_CLIP, W_WRAP = 0, 1, 2, 3

BORDER_TYPES = {
    "none": 0,
    "thin": 1,
    "hair": 2,
    "dotted": 3,
    "dashed": 4,
    "dashDot": 5,
    "dashDotDot": 6,
    "double": 7,
    "medium": 8,
    "mediumDashed": 9,
    "mediumDashDot": 10,
    "mediumDashDotDot": 11,
    "slantDashDot": 12,
    "thick": 13,
}

HORIZONTAL = {
    "left": H_LEFT,
    "center": H_CENTER,
    "right": H_RIGHT,
    "justify": H_JUST,
    "general": H_UNSPEC,
    "fill": H_UNSPEC,
    "centerContinuous": H_CENTER,
    "distributed": H_JUST,
}
VERTICAL = {
    "top": V_TOP,
    "center": V_MIDDLE,
    "middle": V_MIDDLE,
    "bottom": V_BOTTOM,
    "justify": V_UNSPEC,
    "distributed": V_UNSPEC,
}

EXCEL_EPOCH = datetime(1899, 12, 30)
HIGHLIGHT_BG = "#FFF2CC"

_CHAR_TO_PX = 7
_CHAR_PAD = 5
_PT_TO_PX = 96 / 72


def _rgb(color: Any) -> Optional[str]:
    """openpyxl 颜色对象 → '#RRGGBB'。"""
    if color is None:
        return None
    try:
        if getattr(color, "type", None) == "rgb" and color.rgb:
            value = str(color.rgb).upper()
            if len(value) == 8:  # AARRGGBB
                value = value[2:]
            if len(value) == 6 and value != "00000000":
                return f"#{value}"
        if getattr(color, "type", None) == "indexed":
            return None
    except Exception:
        return None
    return None


def _to_serial(value: datetime | date | time) -> float:
    """Excel 1900 日期系统序列号。"""
    if isinstance(value, time):
        return (
            value.hour * 3600 + value.minute * 60 + value.second + value.microsecond / 1e6
        ) / 86400
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day)
    delta = value - EXCEL_EPOCH
    return delta.days + delta.seconds / 86400 + delta.microseconds / 86400 / 1e6


def _side_style(side: Any) -> Optional[dict[str, Any]]:
    if side is None or not side.style:
        return None
    style = BORDER_TYPES.get(side.style)
    if not style:
        return None
    color = _rgb(side.color) or "#000000"
    return {"s": style, "cl": {"rgb": color}}


def _extract_style(cell: Any) -> dict[str, Any]:
    """openpyxl 单元格样式 → IStyleData。"""
    style: dict[str, Any] = {}

    font = cell.font
    if font is not None:
        if font.name:
            style["ff"] = str(font.name)
        if font.size:
            style["fs"] = float(font.size)
        if font.bold:
            style["bl"] = 1
        if font.italic:
            style["it"] = 1
        if font.underline and font.underline != "none":
            style["ul"] = {"s": 1}
        if font.strike:
            style["st"] = {"s": 1}
        color = _rgb(font.color)
        if color:
            style["cl"] = {"rgb": color}

    fill = cell.fill
    if fill is not None and getattr(fill, "fill_type", None) == "solid":
        bg = _rgb(fill.fgColor)
        if bg:
            style["bg"] = {"rgb": bg}

    border = cell.border
    if border is not None:
        bd: dict[str, Any] = {}
        for key, side in (("t", border.top), ("b", border.bottom),
                          ("l", border.left), ("r", border.right)):
            parsed = _side_style(side)
            if parsed:
                bd[key] = parsed
        if bd:
            style["bd"] = bd

    align = cell.alignment
    if align is not None:
        if align.horizontal:
            ht = HORIZONTAL.get(str(align.horizontal))
            if ht:
                style["ht"] = ht
        if align.vertical:
            vt = VERTICAL.get(str(align.vertical))
            if vt:
                style["vt"] = vt
        if align.wrap_text:
            style["tb"] = W_WRAP
        if align.text_rotation:
            style["tr"] = {"a": int(align.text_rotation)}

    number_format = cell.number_format
    if number_format and number_format != "General":
        style["n"] = {"pattern": str(number_format)}

    return style


def _cell_payload(cell: Any) -> Optional[dict[str, Any]]:
    """openpyxl 单元格值 → ICellData。"""
    value = cell.value
    if value is None:
        return None

    if isinstance(value, str) and value.startswith("="):
        return {"f": value, "v": None}

    if isinstance(value, bool):
        return {"v": value, "t": CV_BOOLEAN}
    if isinstance(value, (int, float)):
        return {"v": float(value), "t": CV_NUMBER}
    if isinstance(value, (datetime, date, time)):
        return {"v": _to_serial(value), "t": CV_NUMBER}
    return {"v": str(value), "t": CV_STRING}


def workbook_to_snapshot(
    workbook: Workbook,
    *,
    workbook_id: str = "invoice-workbook",
    workbook_name: str = "Customs Invoice",
    highlight: Optional[dict[str, list[str]]] = None,
    locale: str = "zhCN",
) -> dict[str, Any]:
    """把 openpyxl 工作簿转成 Univer IWorkbookData。

    Args:
        workbook: 已构建好的工作簿（含公式）。
        workbook_id: 快照 id。
        workbook_name: 显示名。
        highlight: {工作表名: ["J9", ...]} 需要黄色高亮的可编辑单元格。
        locale: Univer 语言。

    Returns:
        Univer IWorkbookData 字典。
    """
    highlight = highlight or {}
    styles: dict[str, dict[str, Any]] = {}
    style_index: dict[str, str] = {}

    def register_style(raw: dict[str, Any]) -> Optional[str]:
        if not raw:
            return None
        key = json.dumps(raw, sort_keys=True, ensure_ascii=False)
        cached = style_index.get(key)
        if cached:
            return cached
        style_id = f"s{len(style_index)}"
        style_index[key] = style_id
        styles[style_id] = raw
        return style_id

    sheets: dict[str, dict[str, Any]] = {}
    sheet_order: list[str] = []

    for index, ws in enumerate(workbook.worksheets):
        sheet_id = f"sheet{index}"
        sheet_order.append(sheet_id)

        max_row = ws.max_row or 1
        max_col = ws.max_column or 1
        row_count = max(max_row + 5, 40)
        col_count = max(max_col + 3, 16)

        merged_top_left: dict[tuple[int, int], tuple[int, int, int, int]] = {}
        merge_data: list[dict[str, int]] = []
        skip: set[tuple[int, int]] = set()
        for merged in ws.merged_cells.ranges:
            merge_data.append(
                {
                    "startRow": merged.min_row - 1,
                    "startColumn": merged.min_col - 1,
                    "endRow": merged.max_row - 1,
                    "endColumn": merged.max_col - 1,
                    "rangeType": 0,
                }
            )
            merged_top_left[(merged.min_row - 1, merged.min_col - 1)] = (
                merged.min_row, merged.min_col, merged.max_row, merged.max_col
            )
            for r in range(merged.min_row, merged.max_row + 1):
                for c in range(merged.min_col, merged.max_col + 1):
                    if (r, c) != (merged.min_row, merged.min_col):
                        skip.add((r - 1, c - 1))

        highlights = {coord.upper() for coord in highlight.get(ws.title, [])}

        cell_data: dict[int, dict[int, dict[str, Any]]] = {}
        for row in ws.iter_rows(min_row=1, max_row=max_row, min_col=1, max_col=max_col):
            for cell in row:
                if isinstance(cell, MergedCell):
                    continue
                r, c = cell.row - 1, cell.column - 1
                if (r, c) in skip:
                    continue

                payload = _cell_payload(cell)
                style = _extract_style(cell)
                if cell.coordinate.upper() in highlights:
                    style = dict(style)
                    style["bg"] = {"rgb": HIGHLIGHT_BG}
                style_id = register_style(style)
                if payload is None and style_id is None:
                    continue

                entry: dict[str, Any] = {}
                if payload:
                    entry.update(payload)
                if style_id:
                    entry["s"] = style_id
                if entry:
                    cell_data.setdefault(r, {})[c] = entry

        # 合并区域左上角若没有样式，用普通边框兜底，避免渲染空洞
        for (r, c), (min_r, min_c, max_r, max_c) in merged_top_left.items():
            target = cell_data.get(r, {}).get(c)
            if target is None and 0 <= r < row_count and 0 <= c < col_count:
                cell_data.setdefault(r, {})[c] = {"v": ""}

        # 合并区域的边框：Univer 要求合并区最外圈的每个单元格都带同样的边框
        # 样式（pro/issues/344），左上角的样式不会自动延伸到整个合并区。
        # 因此把左上角样式复制到合并区内所有单元格（0 基索引）。
        for (r, c), (min_r, min_c, max_r, max_c) in merged_top_left.items():
            top_entry = cell_data.get(r, {}).get(c)
            top_style = top_entry.get("s") if top_entry else None
            if top_style is None:
                continue
            for rr in range(min_r - 1, max_r):
                for cc in range(min_c - 1, max_c):
                    if (rr, cc) == (r, c):
                        continue
                    if 0 <= rr < row_count and 0 <= cc < col_count:
                        cell_data.setdefault(rr, {})[cc] = {"s": top_style}

        # 列宽 / 行高
        column_data: dict[int, dict[str, Any]] = {}
        for letter, dim in ws.column_dimensions.items():
            if not dim.width:
                continue
            try:
                col_idx = openpyxl.utils.column_index_from_string(letter) - 1
            except Exception:
                continue
            column_data[col_idx] = {"w": int(round(dim.width * _CHAR_TO_PX + _CHAR_PAD))}

        row_data: dict[int, dict[str, Any]] = {}
        for row_idx, dim in ws.row_dimensions.items():
            if not dim.height:
                continue
            row_data[int(row_idx) - 1] = {"h": int(round(dim.height * _PT_TO_PX))}

        default_col = ws.sheet_format.defaultColWidth or 8.43
        default_row = ws.sheet_format.defaultRowHeight or 14.5

        sheets[sheet_id] = {
            "id": sheet_id,
            "name": ws.title,
            "tabColor": "",
            # 保留模板里的隐藏状态（空运/陆运模板自带隐藏的空 Sheet1）
            "hidden": 1 if ws.sheet_state != "visible" else 0,
            "freeze": {"xSplit": 0, "ySplit": 0, "startRow": 0, "startColumn": 0},
            "rowCount": row_count,
            "columnCount": col_count,
            "zoomRatio": 1,
            "scrollTop": 0,
            "scrollLeft": 0,
            "defaultColumnWidth": int(round(default_col * _CHAR_TO_PX + _CHAR_PAD)),
            "defaultRowHeight": int(round(default_row * _PT_TO_PX)),
            "mergeData": merge_data,
            "cellData": {str(r): {str(c): v for c, v in cols.items()}
                         for r, cols in cell_data.items()},
            "rowData": {str(r): v for r, v in row_data.items()},
            "columnData": {str(c): v for c, v in column_data.items()},
            "rowHeader": {"width": 46},
            "columnHeader": {"height": 24},
            "showGridlines": 1,
            "rightToLeft": 0,
        }

    return {
        "id": workbook_id,
        "name": workbook_name,
        "appVersion": "1.0.0",
        "locale": locale,
        "styles": styles,
        "sheetOrder": sheet_order,
        "sheets": sheets,
    }


def snapshot_digest(snapshot: dict[str, Any]) -> str:
    """快照内容指纹，便于调试。"""
    payload = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()
