# -*- coding: utf-8 -*-
"""第二类账号：发票明细查看与核对。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session
from ..auth import audit, require_csrf, require_type2
from ..models import (
    CHECK_FAILED,
    CHECK_LABELS,
    CHECK_PASSED,
    CHECK_PENDING,
    Invoice,
    InvoiceItem,
    User,
    get_db,
)
from ..templates_setup import templates

router = APIRouter(tags=["details"])


@router.get("/details")
async def detail_list(
    request: Request,
    invoice_id: int = 0,
    q: str = "",
    check_status: str = "",
    page: int = 1,
    user: User = Depends(require_type2),
    db: Session = Depends(get_db),
) -> object:
    inv_query = db.query(Invoice)
    if check_status in (CHECK_PENDING, CHECK_PASSED, CHECK_FAILED):
        inv_query = inv_query.filter(Invoice.check_status == check_status)
    invoices = (
        inv_query.order_by(Invoice.uploaded_at.desc(), Invoice.id.desc())
        .limit(500)
        .all()
    )

    counts = {
        CHECK_PENDING: db.query(Invoice).filter(Invoice.check_status == CHECK_PENDING).count(),
        CHECK_PASSED: db.query(Invoice).filter(Invoice.check_status == CHECK_PASSED).count(),
        CHECK_FAILED: db.query(Invoice).filter(Invoice.check_status == CHECK_FAILED).count(),
    }

    query = db.query(InvoiceItem)
    if invoice_id:
        query = query.filter(InvoiceItem.invoice_id == invoice_id)
    elif check_status in (CHECK_PENDING, CHECK_PASSED, CHECK_FAILED):
        # 按核对状态筛选时，明细表只显示这些发票的行
        filtered_ids = [i.id for i in invoices]
        if not filtered_ids:
            query = query.filter(InvoiceItem.invoice_id.is_(None))
        else:
            query = query.filter(InvoiceItem.invoice_id.in_(filtered_ids))
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(
            or_(
                InvoiceItem.description.ilike(like),
                InvoiceItem.fabric_type.ilike(like),
                InvoiceItem.po_no.ilike(like),
                InvoiceItem.colour.ilike(like),
            )
        )

    total = query.count()
    page = max(page, 1)
    size = 50
    rows = (
        query.order_by(InvoiceItem.invoice_id.desc(), InvoiceItem.row.asc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    pages = max((total + size - 1) // size, 1)

    current = next((i for i in invoices if i.id == invoice_id), None)
    if current is None and invoice_id:
        current = db.get(Invoice, invoice_id)

    return templates.TemplateResponse(
        request,
        "details.html",
        {
            "user": user,
            "invoices": invoices,
            "current": current,
            "rows": rows,
            "invoice_id": invoice_id,
            "q": q,
            "check_status": check_status,
            "counts": counts,
            "page": page,
            "pages": pages,
            "total": total,
            "flash": request.session.pop("flash", None),
        },
    )


@router.post("/details/{invoice_id}/check")
async def detail_check(
    request: Request,
    invoice_id: int,
    status: str = Form(...),
    note: str = Form(""),
    user: User = Depends(require_type2),
    db: Session = Depends(get_db),
    _csrf: None = Depends(require_csrf),
) -> object:
    if status not in (CHECK_PASSED, CHECK_FAILED):
        request.session["flash"] = {
            "level": "danger",
            "message": "无效的核对状态",
        }
        return RedirectResponse(f"/details?invoice_id={invoice_id}", status_code=303)

    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        request.session["flash"] = {"level": "warning", "message": "发票不存在"}
        return RedirectResponse("/details", status_code=303)

    note = note.strip()
    if status == CHECK_FAILED and not note:
        request.session["flash"] = {
            "level": "warning",
            "message": "标记问题时请填写问题说明，便于第一类账号修复",
        }
        return RedirectResponse(f"/details?invoice_id={invoice_id}", status_code=303)

    invoice.check_status = status
    invoice.check_note = note
    invoice.checked_by = user.username
    invoice.checked_at = datetime.utcnow()
    db.commit()

    audit(
        db,
        user,
        "check",
        f"{invoice.invoice_no} -> {CHECK_LABELS.get(status, status)}"
        + (f" | {note}" if note else ""),
    )
    request.session["flash"] = {
        "level": "success" if status == CHECK_PASSED else "warning",
        "message": f"{invoice.invoice_no} 已标记为「{CHECK_LABELS.get(status, status)}」",
    }
    return RedirectResponse(f"/details?invoice_id={invoice_id}", status_code=303)
