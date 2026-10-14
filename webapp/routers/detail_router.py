# -*- coding: utf-8 -*-
"""第二类账号：发票明细查看。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..auth import require_type2
from ..models import Invoice, InvoiceItem, User, get_db
from ..templates_setup import templates

router = APIRouter(tags=["details"])


@router.get("/details")
async def detail_list(
    request: Request,
    invoice_id: int = 0,
    q: str = "",
    page: int = 1,
    user: User = Depends(require_type2),
    db: Session = Depends(get_db),
) -> object:
    invoices = (
        db.query(Invoice)
        .order_by(Invoice.uploaded_at.desc(), Invoice.id.desc())
        .limit(500)
        .all()
    )

    query = db.query(InvoiceItem)
    if invoice_id:
        query = query.filter(InvoiceItem.invoice_id == invoice_id)
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
            "page": page,
            "pages": pages,
            "total": total,
        },
    )
