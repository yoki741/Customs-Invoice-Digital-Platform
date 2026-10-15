# -*- coding: utf-8 -*-
"""编辑 / 导出用例编排。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from loguru import logger
from sqlalchemy.orm import Session

from core.config import OUTPUT_DIR, ensure_dirs
from core.exporter import BuildResult, Exporter, ManualFields
from core.source_reader import SourceData, read_source_file
from .converters.field_map import editable_cells, validate_form
from .converters.univer_snapshot import workbook_to_snapshot
from .models import ExportJob, Invoice, User

_exporter: Optional[Exporter] = None


def get_exporter() -> Exporter:
    """导出器单例（模板 / 单价表只读一次）。"""
    global _exporter
    if _exporter is None:
        _exporter = Exporter()
    return _exporter


def load_sources(invoices: list[Invoice]) -> list[SourceData]:
    """按发票记录回读源文件。"""
    sources: list[SourceData] = []
    for invoice in invoices:
        path = Path(invoice.source_file)
        if not path.exists():
            raise ValueError(f"源文件不存在: {invoice.source_filename}")
        sources.append(read_source_file(path))
    return sources


def group_sources(sources: list[SourceData]) -> list[list[SourceData]]:
    """按 Delivery to 分组（与 EXE 的分组规则一致）。"""
    groups: dict[str, list[SourceData]] = {}
    for source in sources:
        groups.setdefault(source.delivery_to or "UNKNOWN", []).append(source)
    return list(groups.values())


def _carrier_for(sources: list[SourceData]) -> Path:
    """VBA 载体：取第一个源文件。"""
    return Path(sources[0].file_path)


def build_for_edit(invoices: list[Invoice]) -> tuple[BuildResult, list[SourceData]]:
    """构建编辑页预览工作簿（人工字段留空）。"""
    if not invoices:
        raise ValueError("没有选中的发票")
    sources = load_sources(invoices)
    groups = group_sources(sources)
    if len(groups) > 1:
        names = [g[0].delivery_to or "UNKNOWN" for g in groups]
        raise ValueError(
            "选中的发票 Delivery to 不一致，无法合并导出："
            + " / ".join(names)
        )
    exporter = get_exporter()
    result = exporter.build(groups[0], ManualFields())
    if not result.ok:
        raise ValueError("；".join(result.messages) or "构建失败")
    return result, sources


def build_snapshot(invoices: list[Invoice]) -> dict[str, Any]:
    """生成编辑页所需的完整数据。"""
    result, sources = build_for_edit(invoices)

    first = sources[0]
    cells = editable_cells(result.transport, result.container_row)
    snapshot = workbook_to_snapshot(
        result.workbook,
        workbook_id=f"invoice-{invoices[0].id}",
        workbook_name=result.file_stem,
        highlight=cells,
    )
    result.workbook.close()

    fields = [
        {
            "key": f.key,
            "label": f.label,
            "sheet": f.sheet,
            "cell": f.cell,
            "hint": f.hint,
            "group": f.group,
            "multiline": f.multiline,
            "required": f.required,
            "applicable": _field_applicable(f.key, result),
        }
        for f in _fields_for(result)
    ]

    return {
        "snapshot": snapshot,
        "editable": cells,
        "fields": fields,
        "manual": _stored_manual(invoices),
        "meta": {
            "invoice_ids": [i.id for i in invoices],
            "invoice_nos": [i.invoice_no for i in invoices],
            "transport": result.transport,
            "company": result.company,
            "file_stem": result.file_stem,
            "customs_weight": result.customs_weight,
            "total_amount": result.total_amount,
            "container_row": result.container_row,
            "delivery_to": first.delivery_to,
            "ship_mode": first.ship_mode,
            "source_filename": first.file_name,
        },
    }


def _fields_for(result: BuildResult):
    from .converters.field_map import EDITABLE_FIELDS, EXTRA_FIELDS

    return list(EDITABLE_FIELDS) + list(EXTRA_FIELDS)


def _stored_manual(invoices: list[Invoice]) -> dict[str, str]:
    """读取发票上持久化的人工字段（取第一张有值的）。"""
    for invoice in invoices:
        try:
            data = json.loads(invoice.manual_values or "{}")
        except (TypeError, ValueError):
            continue
        if data:
            return {str(k): str(v) for k, v in data.items() if v}
    return {}


def save_manual_values(db: Session, invoices: list[Invoice], values: dict[str, str]) -> None:
    """把人工字段写回组内所有发票（合并导出共享同一份值）。"""
    payload = json.dumps(values, ensure_ascii=False)
    for invoice in invoices:
        invoice.manual_values = payload
    db.commit()


def _field_applicable(key: str, result: BuildResult) -> bool:
    if key == "port_loading":
        return result.transport == "陆运"
    if key == "container_info":
        return result.container_row is not None
    if key == "flight_no":
        return result.transport == "空运"
    return True


def export_invoices(
    db: Session,
    invoices: list[Invoice],
    form_values: dict[str, Any],
    user: Optional[User] = None,
) -> tuple[Path, dict[str, Any]]:
    """生成最终 .xlsm 并记录导出历史。

    Returns:
        (文件路径, 结果信息)
    """
    values, errors = validate_form(form_values)
    if errors:
        raise ValueError("；".join(errors))

    sources = load_sources(invoices)
    groups = group_sources(sources)
    if len(groups) > 1:
        raise ValueError("选中的发票 Delivery to 不一致，无法合并导出")

    exporter = get_exporter()
    manual = ManualFields.from_dict(values)
    result = exporter.build(groups[0], manual)
    if not result.ok:
        raise ValueError("；".join(result.messages) or "构建失败")

    ensure_dirs()
    folder = _safe(values["bill_no"]) or "未命名"
    out_dir = OUTPUT_DIR / folder
    out_path = out_dir / f"{result.file_stem}.xlsm"
    exporter.save_xlsm(result.workbook, out_path, _carrier_for(sources))

    info = {
        "filename": out_path.name,
        "path": str(out_path),
        "folder": folder,
        "customs_weight": result.customs_weight,
        "total_amount": result.total_amount,
        "messages": result.messages,
    }

    job = ExportJob(
        invoice_ids=json.dumps([i.id for i in invoices]),
        form_values=json.dumps(values, ensure_ascii=False),
        filename=f"{folder}/{out_path.name}",
        success=True,
        message="；".join(result.messages),
        customs_weight=result.customs_weight,
        total_amount=result.total_amount,
        created_by=user.username if user else None,
    )
    db.add(job)
    # 导出成功即持久化人工字段，下次打开编辑页自动回填
    for invoice in invoices:
        invoice.manual_values = json.dumps(values, ensure_ascii=False)
    db.commit()
    logger.info(
        "导出 {} by {}", out_path.name, user.username if user else "-"
    )
    return out_path, info


def record_failure(
    db: Session,
    invoices: list[Invoice],
    form_values: dict[str, Any],
    message: str,
    user: Optional[User] = None,
) -> None:
    """记录失败的导出尝试。"""
    try:
        db.add(
            ExportJob(
                invoice_ids=json.dumps([i.id for i in invoices]),
                form_values=json.dumps(form_values or {}, ensure_ascii=False),
                filename="",
                success=False,
                message=message[:2000],
                created_by=user.username if user else None,
            )
        )
        db.commit()
    except Exception as exc:  # pragma: no cover
        logger.warning("记录导出失败日志出错: {}", exc)


def _safe(name: str) -> str:
    import re

    return re.sub(r'[\\/:*?"<>|]', "_", (name or "").strip())
