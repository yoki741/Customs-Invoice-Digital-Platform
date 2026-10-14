# -*- coding: utf-8 -*-
"""上传文件 → 数据库。"""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Optional

from loguru import logger
from sqlalchemy.orm import Session

from core.config import UPLOAD_DIR, ensure_dirs
from core.price_table import get_company_name, get_transport_type
from core.source_reader import SourceData, read_source_file
from .models import Invoice, InvoiceItem, User


def _safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "upload.xlsm"


def save_upload(file_name: str, content: bytes) -> Path:
    """把上传内容写入 uploads 目录，返回路径。"""
    ensure_dirs()
    target = UPLOAD_DIR / _safe_name(Path(file_name).name)
    # 同名不同内容时加内容指纹，避免覆盖别人的数据源
    if target.exists():
        digest = hashlib.sha1(content).hexdigest()[:8]
        candidate = UPLOAD_DIR / f"{target.stem}_{digest}{target.suffix}"
        if candidate.exists() and candidate.read_bytes() == content:
            return candidate
        target = candidate
    target.write_bytes(content)
    return target


def import_source_file(
    db: Session,
    file_path: Path,
    user: Optional[User] = None,
    replace: bool = True,
) -> tuple[Invoice, bool]:
    """解析源文件并入库。

    Args:
        db: 数据库会话。
        file_path: 已保存到 uploads 的源文件。
        user: 上传人。
        replace: 同 Invoice No. 已存在时是否替换。

    Returns:
        (Invoice 记录, 是否为新建)

    Raises:
        ValueError: 解析失败或不允许重复。
    """
    try:
        data = read_source_file(file_path)
    except Exception as exc:
        raise ValueError(f"解析失败: {exc}") from exc

    if not data.invoice_no:
        raise ValueError("源文件缺少 Invoice No.（InputTable!C5）")

    existing = (
        db.query(Invoice).filter(Invoice.invoice_no == data.invoice_no).one_or_none()
    )
    created = existing is None

    if existing and not replace:
        raise ValueError(f"Invoice No. {data.invoice_no} 已存在")

    invoice = existing or Invoice()
    _fill_invoice(invoice, data, file_path, user)

    # 走 ORM 关系清空，避免 query.delete() 与已加载集合冲突
    invoice.items.clear()
    for item in data.items:
        invoice.items.append(
            InvoiceItem(
                row=item.row,
                line_no=item.line_no,
                po_no=item.po_no,
                description=item.description,
                colour=item.colour,
                size=item.size,
                fabric_type=item.fabric_type,
                carton=item.carton,
                net_weight=item.net_weight,
                gross_weight=item.gross_weight,
                length=item.length,
                quantity=item.quantity,
                unit_price=item.unit_price,
                amount=item.amount,
            )
        )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    logger.info(
        "导入 {} -> invoice_no={} 明细{}条 ({})",
        file_path.name,
        data.invoice_no,
        len(data.items),
        "新建" if created else "替换",
    )
    return invoice, created


def _fill_invoice(invoice: Invoice, data: SourceData, file_path: Path, user: Optional[User]) -> None:
    invoice.invoice_no = data.invoice_no
    invoice.invoice_date = data.invoice_date
    invoice.bill_to = data.bill_to
    invoice.delivery_to = data.delivery_to
    invoice.supplier_code = data.supplier_code
    invoice.company = get_company_name(data.delivery_to) or get_company_name(data.bill_to)
    invoice.transport = get_transport_type(data.ship_mode, data.file_name)
    invoice.ship_mode = data.ship_mode
    invoice.ship_term = data.ship_term
    invoice.vessel_flight = data.vessel_flight
    invoice.etd = data.etd
    invoice.eta = data.eta
    invoice.container_number = data.container_number
    invoice.seal_number = data.seal_number
    invoice.destination = data.destination
    invoice.currency = data.currency
    invoice.customs_agent = data.customs_agent
    invoice.origin_place = data.origin_place
    invoice.origin_country = data.origin_country
    invoice.rolls = data.rolls
    invoice.net_weight = data.net_weight
    invoice.gross_weight = data.gross_weight
    invoice.total_length = data.total_length
    invoice.total_lbs = data.total_lbs
    invoice.total_amount = data.total_amount
    invoice.item_count = len(data.items)
    invoice.source_filename = data.file_name
    invoice.source_file = str(file_path)
    if user is not None:
        invoice.uploaded_by = user.username


def bulk_import(db: Session, files: list[Path], user: Optional[User] = None) -> list[dict]:
    """批量导入（目录下所有 .xlsm）。"""
    results = []
    for path in files:
        try:
            invoice, created = import_source_file(db, path, user)
            results.append(
                {"file": path.name, "ok": True, "invoice_no": invoice.invoice_no,
                 "message": "新建" if created else "替换"}
            )
        except Exception as exc:
            db.rollback()
            logger.exception("导入失败 {}", path)
            results.append({"file": path.name, "ok": False, "message": str(exc)})
    return results


def copy_into_uploads(path: Path) -> Path:
    """把任意目录下的文件复制进 uploads（供目录扫描导入）。"""
    ensure_dirs()
    target = UPLOAD_DIR / _safe_name(path.name)
    if target.resolve() != path.resolve():
        shutil.copy2(path, target)
    return target
