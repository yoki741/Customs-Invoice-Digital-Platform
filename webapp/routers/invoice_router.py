# -*- coding: utf-8 -*-
"""第一类账号：发票列表、Excel 编辑页、导出。"""

from __future__ import annotations

import io

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..auth import audit, require_type1
from ..converters.field_map import EDITABLE_FIELDS, EXTRA_FIELDS
from ..models import Invoice, User, get_db
from ..service import (
    build_for_edit,
    build_snapshot,
    export_invoices,
    record_failure,
)
from ..templates_setup import templates

router = APIRouter(tags=["invoices"])


class ExportBody(BaseModel):
    form: dict = {}


def _parse_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for part in raw.replace("%2C", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return ids


def _load_invoices(db: Session, ids: list[int]) -> list[Invoice]:
    if not ids:
        return []
    rows = db.query(Invoice).filter(Invoice.id.in_(ids)).all()
    order = {v: i for i, v in enumerate(ids)}
    rows.sort(key=lambda r: order.get(r.id, 1 << 30))
    return rows


@router.get("/invoices")
async def invoice_list(
    request: Request,
    q: str = "",
    transport: str = "",
    company: str = "",
    page: int = 1,
    user: User = Depends(require_type1),
    db: Session = Depends(get_db),
) -> object:
    query = db.query(Invoice)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(
            or_(
                Invoice.invoice_no.ilike(like),
                Invoice.bill_to.ilike(like),
                Invoice.delivery_to.ilike(like),
                Invoice.supplier_code.ilike(like),
                Invoice.source_filename.ilike(like),
            )
        )
    if transport:
        query = query.filter(Invoice.transport == transport)
    if company:
        query = query.filter(Invoice.company == company)

    total = query.count()
    page = max(page, 1)
    size = 20
    rows = (
        query.order_by(Invoice.uploaded_at.desc(), Invoice.id.desc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    pages = max((total + size - 1) // size, 1)

    return templates.TemplateResponse(
        request,
        "invoices.html",
        {
            "user": user,
            "rows": rows,
            "q": q,
            "transport": transport,
            "company": company,
            "page": page,
            "pages": pages,
            "total": total,
            "flash": request.session.pop("flash", None),
        },
    )


@router.get("/invoices/edit")
async def invoice_edit(
    request: Request,
    ids: str = "",
    user: User = Depends(require_type1),
    db: Session = Depends(get_db),
) -> object:
    id_list = _parse_ids(ids)
    invoices = _load_invoices(db, id_list)
    if not invoices:
        return templates.TemplateResponse(
            request,
            "message.html",
            {"user": user, "level": "warning", "title": "无法编辑",
             "message": "未找到选中的发票", "back": "/invoices"},
            status_code=404,
        )

    try:
        _, sources = build_for_edit(invoices)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "message.html",
            {"user": user, "level": "warning", "title": "无法编辑",
             "message": str(exc), "back": "/invoices"},
            status_code=400,
        )

    return templates.TemplateResponse(
        request,
        "edit.html",
        {
            "user": user,
            "invoices": invoices,
            "ids": ",".join(str(i.id) for i in invoices),
            "first": sources[0],
            "form_fields": [f for f in EDITABLE_FIELDS] + list(EXTRA_FIELDS),
        },
    )


@router.get("/api/invoices/snapshot")
async def invoice_snapshot(
    ids: str = "",
    user: User = Depends(require_type1),
    db: Session = Depends(get_db),
) -> object:
    invoices = _load_invoices(db, _parse_ids(ids))
    if not invoices:
        return JSONResponse({"ok": False, "error": "未找到发票"}, status_code=404)
    try:
        payload = build_snapshot(invoices)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # pragma: no cover
        logger.exception("生成快照失败")
        return JSONResponse({"ok": False, "error": f"生成失败: {exc}"}, status_code=500)
    payload["ok"] = True
    return JSONResponse(payload)


@router.get("/api/invoices/preview.xlsx")
async def invoice_preview_xlsx(
    ids: str = "",
    user: User = Depends(require_type1),
    db: Session = Depends(get_db),
) -> object:
    """返回预填（人工字段留空）的 .xlsx，供下载或排查。"""
    invoices = _load_invoices(db, _parse_ids(ids))
    if not invoices:
        return JSONResponse({"ok": False, "error": "未找到发票"}, status_code=404)
    try:
        result, _ = build_for_edit(invoices)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    buffer = io.BytesIO()
    result.workbook.save(buffer)
    result.workbook.close()
    name = f"{result.file_stem}_preview.xlsx"
    return Response(
        content=buffer.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.post("/api/invoices/export")
async def invoice_export(
    body: ExportBody,
    ids: str = "",
    user: User = Depends(require_type1),
    db: Session = Depends(get_db),
) -> object:
    invoices = _load_invoices(db, _parse_ids(ids))
    if not invoices:
        return JSONResponse({"ok": False, "error": "未找到发票"}, status_code=404)

    try:
        path, info = export_invoices(db, invoices, body.form, user)
    except ValueError as exc:
        record_failure(db, invoices, body.form, str(exc), user)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # pragma: no cover
        logger.exception("导出失败")
        record_failure(db, invoices, body.form, f"导出异常: {exc}", user)
        return JSONResponse({"ok": False, "error": f"导出失败: {exc}"}, status_code=500)

    audit(db, user, "export", f"导出 {info['filename']}")
    from urllib.parse import quote

    download = (
        f"/api/download?folder={quote(info['folder'])}&name={quote(info['filename'])}"
    )
    return JSONResponse({"ok": True, **info, "download": download})


@router.get("/api/download")
async def download(
    folder: str = "",
    name: str = "",
    user: User = Depends(require_type1),
) -> object:
    from core.config import OUTPUT_DIR

    base = OUTPUT_DIR.resolve()
    target = (base / folder / name).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return JSONResponse({"ok": False, "error": "非法路径"}, status_code=400)
    if not target.is_file():
        return JSONResponse({"ok": False, "error": "文件不存在"}, status_code=404)
    return FileResponse(
        target,
        media_type="application/vnd.ms-excel.sheet.macroEnabled.12",
        filename=target.name,
    )
