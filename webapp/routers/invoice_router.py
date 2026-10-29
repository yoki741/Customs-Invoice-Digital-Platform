# -*- coding: utf-8 -*-
"""第一类账号：发票列表、Excel 编辑页、导出、文件版本。"""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import case, or_
from sqlalchemy.orm import Session, noload

from ..auth import (
    audit,
    require_admin,
    require_any,
    require_type1,
    require_type1_or_2,
    require_type1_or_3,
    require_type3,
)
from ..timefmt import fmt_local
from ..converters.field_map import EDITABLE_FIELDS, EXTRA_FIELDS, validate_form
from ..models import (
    CHECK_FAILED,
    CHECK_PENDING,
    CHECK_PASSED,
    ROLE_TYPE2,
    ExportFileVersion,
    ExportJob,
    Invoice,
    ShipmentListEntry,
    User,
    get_db,
)
from ..service import (
    ENTRY_EDIT_FIELDS,
    ENTRY_FIELD_LABELS,
    build_for_edit,
    build_snapshot,
    can_edit_ref_id,
    export_invoices,
    first_generation_ids,
    record_failure,
    record_manual_save,
    reset_manual_state,
    save_manual_values,
    update_shipment_entry,
)
from ..templates_setup import templates

router = APIRouter(tags=["invoices"])


class ExportBody(BaseModel):
    form: dict = {}
    shipment: dict = {}
    last_saved_at: str = ""
    force: bool = False


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
    rows = (
        db.query(Invoice)
        .options(noload(Invoice.items))
        .filter(Invoice.id.in_(ids))
        .all()
    )
    order = {v: i for i, v in enumerate(ids)}
    rows.sort(key=lambda r: order.get(r.id, 1 << 30))
    return rows


def invoice_list_context(
    request: Request,
    db: Session,
    user: User,
    *,
    q: str = "",
    transport: str = "",
    company: str = "",
    check_status: str = "",
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
    highlight_ids: Optional[list[int]] = None,
    pending: Optional[list[dict]] = None,
    results: Optional[list[dict]] = None,
) -> dict:
    """构建发票列表页 invoices.html 的渲染上下文（列表 + 数据导入面板共用）。

    Args:
        request: 当前请求（用于取一次性 flash）。
        db: 数据库会话。
        user: 当前登录用户。
        q: 搜索关键字。
        transport: 运输方式筛选。
        company: 公司筛选。
        check_status: 校验状态筛选。
        date_from: 导入日期起（含）。
        date_to: 导入日期止（含）。
        page: 页码，从 1 开始。
        highlight_ids: 需要 3 秒高光的新导入发票 ID。
        pending: 数据导入第一段检出的重复发票待确认列表。
        results: 数据导入结果列表。

    Returns:
        传给模板的上下文字典。
    """
    query = db.query(Invoice).options(noload(Invoice.items))
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
    if check_status in (CHECK_PENDING, CHECK_PASSED, CHECK_FAILED):
        query = query.filter(Invoice.check_status == check_status)
    if date_from.strip():
        try:
            d = datetime.strptime(date_from.strip(), "%Y-%m-%d")
            query = query.filter(Invoice.uploaded_at >= d)
        except ValueError:
            pass
    if date_to.strip():
        try:
            d = datetime.strptime(date_to.strip(), "%Y-%m-%d")
            from datetime import timedelta

            query = query.filter(Invoice.uploaded_at < d + timedelta(days=1))
        except ValueError:
            pass

    total = query.count()
    page = max(page, 1)
    size = 20
    order = []
    if highlight_ids:
        # 新导入的发票置顶展示，保证 3 秒高光出现在当前页
        order.append(case((Invoice.id.in_(highlight_ids), 0), else_=1))
    order += [Invoice.uploaded_at.desc(), Invoice.id.desc()]
    rows = (
        query.order_by(*order)
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    pages = max((total + size - 1) // size, 1)

    return {
        "user": user,
        "rows": rows,
        "q": q,
        "transport": transport,
        "company": company,
        "check_status": check_status,
        "date_from": date_from,
        "date_to": date_to,
        "page": page,
        "pages": pages,
        "total": total,
        "flash": request.session.pop("flash", None),
        "highlight_ids": highlight_ids or [],
        "pending": pending,
        "results": results,
    }


@router.get("/invoices")
async def invoice_list(
    request: Request,
    q: str = "",
    transport: str = "",
    company: str = "",
    check_status: str = "",
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
    user: User = Depends(require_type1),
    db: Session = Depends(get_db),
) -> object:
    return templates.TemplateResponse(
        request,
        "invoices.html",
        invoice_list_context(
            request, db, user,
            q=q, transport=transport, company=company,
            check_status=check_status, date_from=date_from,
            date_to=date_to, page=page,
        ),
    )


@router.get("/invoices/edit")
async def invoice_edit(
    request: Request,
    ids: str = "",
    back: str = "",
    user: User = Depends(require_type1_or_2),
    db: Session = Depends(get_db),
) -> object:
    # 返回列表链接（还原筛选条件），只允许 ?开头的安全查询串
    back_url = back if back.startswith("?") and len(back) < 500 else ""
    back_link = f"/invoices{back_url}" if back_url else "/invoices"
    # 第二类账号没有发票列表页，返回 Shipment List（其编辑入口来源）
    if user.role == ROLE_TYPE2:
        back_link = "/shipments"
    id_list = _parse_ids(ids)
    invoices = _load_invoices(db, id_list)
    if not invoices:
        return templates.TemplateResponse(
            request,
            "message.html",
            {"user": user, "level": "warning", "title": "Cannot edit",
             "message": "No selected invoice was found", "back": back_link},
            status_code=404,
        )

    # 页面只做轻量校验（收货方一致性），真正的构建交给快照接口（带缓存），
    # 避免每次打开编辑页重复解析源文件、构建工作簿
    deliveries = {i.delivery_to or "UNKNOWN" for i in invoices}
    if len(deliveries) > 1:
        return templates.TemplateResponse(
            request,
            "message.html",
            {"user": user, "level": "warning", "title": "Cannot edit",
             "message": "The selected invoices have different Delivery to values "
                        "and cannot be merged for export: "
                        + " / ".join(sorted(deliveries)), "back": back_link},
            status_code=400,
        )

    return templates.TemplateResponse(
        request,
        "edit.html",
        {
            "user": user,
            "invoices": invoices,
            "ids": ",".join(str(i.id) for i in invoices),
            "first": invoices[0],
            "back_link": back_link,
            "form_fields": [f for f in EDITABLE_FIELDS] + list(EXTRA_FIELDS),
        },
    )


@router.get("/api/invoices/snapshot")
async def invoice_snapshot(
    ids: str = "",
    user: User = Depends(require_type1_or_2),
    db: Session = Depends(get_db),
) -> object:
    invoices = _load_invoices(db, _parse_ids(ids))
    if not invoices:
        return JSONResponse({"ok": False, "error": "Invoice not found"}, status_code=404)
    try:
        payload = build_snapshot(invoices)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # pragma: no cover
        logger.exception("生成快照失败")
        return JSONResponse({"ok": False, "error": f"Build failed: {exc}"}, status_code=500)
    payload["ok"] = True
    return JSONResponse(payload)


@router.post("/api/invoices/export")
async def invoice_export(
    body: ExportBody,
    ids: str = "",
    user: User = Depends(require_type1_or_2),
    db: Session = Depends(get_db),
) -> object:
    invoices = _load_invoices(db, _parse_ids(ids))
    if not invoices:
        return JSONResponse({"ok": False, "error": "Invoice not found"}, status_code=404)

    try:
        path, info = export_invoices(db, invoices, body.form, user, shipment=body.shipment)
    except ValueError as exc:
        record_failure(db, invoices, body.form, str(exc), user)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # pragma: no cover
        logger.exception("导出失败")
        record_failure(db, invoices, body.form, f"Export error: {exc}", user)
        return JSONResponse({"ok": False, "error": f"Export failed: {exc}"}, status_code=500)

    audit(db, user, "export", f"Exported {info['filename']}")
    from urllib.parse import quote

    download = (
        f"/api/download?folder={quote(info['folder'])}&name={quote(info['filename'])}"
    )
    return JSONResponse({"ok": True, **info, "download": download})


@router.get("/shipments")
async def shipment_list_page(
    request: Request,
    month: str = "",
    company: str = "",
    mode: str = "",
    q: str = "",
    page: int = 1,
    user: User = Depends(require_any),
    db: Session = Depends(get_db),
) -> object:
    """装柜列表（Shipment List），全部角色可见（第二类账号从此进入报关发票编辑）。"""
    from core.shipment_list import month_path

    query = db.query(ShipmentListEntry)
    if month:
        query = query.filter(ShipmentListEntry.month == month)
    if company in ("Eastex", "Vanco"):
        query = query.filter(ShipmentListEntry.est_vco == company)
    if mode in ("AIR", "SEA", "TRUCK"):
        query = query.filter(ShipmentListEntry.mode == mode)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(
            or_(
                ShipmentListEntry.custom_no.ilike(like),
                ShipmentListEntry.blading.ilike(like),
                ShipmentListEntry.fty_inv.ilike(like),
                ShipmentListEntry.shipper.ilike(like),
                ShipmentListEntry.description.ilike(like),
            )
        )

    total = query.count()
    page = max(page, 1)
    size = 50
    rows = (
        query.order_by(
            ShipmentListEntry.date.desc(), ShipmentListEntry.id.desc()
        )
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    # 报关发票列：按 job_id 批量回查文件名与存在性
    job_ids = {r.job_id for r in rows if r.job_id}
    jobs = (
        {
            j.id: j
            for j in db.query(ExportJob).filter(ExportJob.id.in_(job_ids)).all()
        }
        if job_ids
        else {}
    )
    from core.config import OUTPUT_DIR

    first_ids: dict[tuple[str, str], str] = {}
    for r in rows:
        job = jobs.get(r.job_id) if r.job_id else None
        folder = name = ""
        if job and job.filename:
            folder, _, name = job.filename.partition("/")
        r.file_folder = folder
        r.file_name = name
        r.file_exists = bool(name) and (OUTPUT_DIR / folder / name).is_file()
        # 报关发票编辑入口（第二类账号）：进入第一次生成该文件时的发票组合，
        # 保证编辑页与首次生成一致
        key = (folder, name)
        if name and key not in first_ids:
            first_ids[key] = first_generation_ids(db, folder, name)
        edit_ids = first_ids.get(key, "") if name else ""
        if name and not edit_ids:
            try:
                edit_ids = ",".join(
                    str(int(x)) for x in json.loads(r.invoice_ids or "[]")
                )
            except (TypeError, ValueError):
                edit_ids = ""
        r.edit_ids = edit_ids
    months = [
        r[0]
        for r in db.query(ShipmentListEntry.month)
        .filter(ShipmentListEntry.month != "")
        .distinct()
        .order_by(ShipmentListEntry.month.desc())
        .all()
    ]
    current_month = month if month in months else (months[0] if months else "")
    path = month_path(current_month) if current_month else None
    return templates.TemplateResponse(
        request,
        "shipments.html",
        {
            "user": user,
            "rows": rows,
            "months": months,
            "month": month,
            "company": company,
            "mode": mode,
            "q": q,
            "page": page,
            "pages": max((total + size - 1) // size, 1),
            "total": total,
            "current_month": current_month,
            "download_name": path.name if path and path.exists() else "",
            "download_url": (
                f"/api/shipment-list/download?month={current_month}"
                if current_month
                else ""
            ),
        },
    )


@router.get("/api/shipment-list/download")
async def shipment_list_download(
    month: str = "",
    user: User = Depends(require_type1_or_3),
) -> object:
    """下载某月的装柜列表 Excel。"""
    import re as _re

    from core.shipment_list import month_path

    if not _re.fullmatch(r"\d{4}-\d{2}", month):
        return JSONResponse({"ok": False, "error": "Month format should be YYYY-MM"}, status_code=400)
    path = month_path(month)
    if not path.is_file():
        return JSONResponse({"ok": False, "error": "Shipment List for this month does not exist"}, status_code=404)
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name,
    )


@router.post("/api/shipments/{entry_id}/ref-id")
async def shipment_ref_id_save(
    entry_id: int,
    ref_id: str = Form(""),
    month: str = Form(""),
    company: str = Form(""),
    mode: str = Form(""),
    q: str = Form(""),
    page: str = Form(""),
    user: User = Depends(require_type3),
    db: Session = Depends(get_db),
) -> object:
    """保存装柜列表某行手动填写的 ID（仅存数据库，不写入 Excel）。

    权限分割：ID 仅第三类账号与管理员可改。
    """
    from urllib.parse import urlencode

    entry = db.get(ShipmentListEntry, entry_id)
    if entry is None:
        return JSONResponse({"ok": False, "error": "Record not found"}, status_code=404)

    entry.ref_id = (ref_id or "").strip()
    entry.updated_at = datetime.utcnow()
    db.commit()
    audit(
        db,
        user,
        "shipment.ref_id",
        f"Shipment ID {entry.custom_no or entry_id} → {entry.ref_id or '(blank)'}",
    )

    params = {
        k: v
        for k, v in (
            ("month", month),
            ("company", company),
            ("mode", mode),
            ("q", q),
            ("page", page),
        )
        if v
    }
    query = urlencode(params)
    return RedirectResponse(f"/shipments?{query}" if query else "/shipments", status_code=303)


@router.post("/api/shipments/{entry_id}/update")
async def shipment_entry_update(
    entry_id: int,
    request: Request,
    user: User = Depends(require_type1_or_3),
    db: Session = Depends(get_db),
) -> object:
    """Shipment List 行内编辑保存：全量提交该行，同步 .xlsm 与发票明细。

    空值 = 清空（所见即所得），与 /api/shipments/info 的「留空保留」语义不同。
    可映射到 .xlsm 单元格的字段（Date/Mode/POL/柜号/Package/GW/NW）改动时
    改写报关发票文件并留修改记录；其余字段平台-only，月度 Excel 照常重建。
    """
    entry = db.get(ShipmentListEntry, entry_id)
    if entry is None:
        return JSONResponse({"ok": False, "error": "Record not found"}, status_code=404)

    form = await request.form()
    payload = {field: form.get(field, "") for field in ENTRY_EDIT_FIELDS}
    try:
        result = update_shipment_entry(db, entry, payload, user)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    detail = "; ".join(
        f"{c['label']} {c['old'] or '(blank)'}→{c['new'] or '(blank)'}" for c in result["changes"]
    )
    audit(
        db,
        user,
        "shipment.update",
        f"Shipment List inline edit {entry.custom_no or entry_id}: {detail or 'no changes'}",
    )
    return JSONResponse(result)


@router.get("/api/shipments/download-files")
async def shipment_files_download(
    ids: str = "",
    user: User = Depends(require_type1_or_3),
    db: Session = Depends(get_db),
) -> object:
    """批量下载选中行的报关发票 .xlsm（zip 内存打包）。

    部分文件缺失时 zip 内附 missing.txt 说明；全部缺失返回 JSON 错误。
    """
    import io
    import zipfile

    from core.config import OUTPUT_DIR

    entry_ids = _parse_ids(ids)
    if not entry_ids:
        return JSONResponse({"ok": False, "error": "Please select the rows to download first"}, status_code=400)
    entries = (
        db.query(ShipmentListEntry)
        .filter(ShipmentListEntry.id.in_(entry_ids))
        .all()
    )
    if not entries:
        return JSONResponse({"ok": False, "error": "Record not found"}, status_code=404)

    job_ids = {e.job_id for e in entries if e.job_id}
    jobs = (
        {
            j.id: j
            for j in db.query(ExportJob).filter(ExportJob.id.in_(job_ids)).all()
        }
        if job_ids
        else {}
    )

    buffer = io.BytesIO()
    missing: list[str] = []
    used_names: set[str] = set()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for entry in entries:
            job = jobs.get(entry.job_id) if entry.job_id else None
            folder = name = ""
            if job and job.filename:
                folder, _, name = job.filename.partition("/")
            path = OUTPUT_DIR / folder / name if name else None
            if path is None or not path.is_file():
                missing.append(f"{entry.custom_no or entry.id}: {name or '(no file generated)'}")
                continue
            arcname = name
            if arcname in used_names:
                arcname = f"{folder}__{name}"
            used_names.add(arcname)
            zf.write(path, arcname)
            audit(
                db,
                user,
                "download",
                f"Batch-downloaded customs invoice {folder}/{name} (shipment #{entry.id})",
            )
        if missing:
            zf.writestr("missing.txt", "\n".join(missing))

    if len(missing) == len(entries):
        return JSONResponse(
            {"ok": False, "error": "No customs invoice file exists for the selected rows"},
            status_code=404,
        )
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="shipment_files_{stamp}.zip"'
        },
    )


@router.post("/api/shipments/info")
async def shipment_info_save(
    ids: str = Form(""),
    ref_id: str = Form(""),
    package: str = Form(""),
    etd: str = Form(""),
    eta: str = Form(""),
    vessel: str = Form(""),
    carrier: str = Form(""),
    container_type: str = Form(""),
    cbm: str = Form(""),
    user: User = Depends(require_type1_or_3),
    db: Session = Depends(get_db),
) -> object:
    """编辑页「装柜列表信息」独立窗口：直接更新装柜记录（无需重新导出）。

    留空字段一律保留原值；ID 仅存数据库不写入 Excel。尚无对应装柜记录
    （这批发票还没导出过）时返回提示，填写内容可在导出时随附写入。
    """
    from core.shipment_list import eta_for, parse_date
    from webapp.service import rebuild_shipment_month

    invoices = _load_invoices(db, _parse_ids(ids))
    if not invoices:
        return JSONResponse({"ok": False, "error": "Invoice not found"}, status_code=400)

    invoice_nos = sorted({i.invoice_no for i in invoices if i.invoice_no})
    company = next((i.company for i in invoices if i.company), "")
    source_key = f"{company}:{','.join(invoice_nos)}" if invoice_nos else ""

    entry = None
    if source_key:
        entry = (
            db.query(ShipmentListEntry)
            .filter(ShipmentListEntry.source_key == source_key)
            .one_or_none()
        )
    if entry is None:
        # 兜底：按发票 id 集合匹配历史记录
        wanted = {i.id for i in invoices}
        for cand in db.query(ShipmentListEntry).order_by(ShipmentListEntry.id.desc()).all():
            try:
                if set(json.loads(cand.invoice_ids or "[]")) == wanted:
                    entry = cand
                    break
            except Exception:
                continue
    if entry is None:
        return JSONResponse(
            {"ok": False, "error": "No shipment record yet: please click \"Generate .xlsm\" first; values filled here will be written along with the export"},
            status_code=404,
        )

    def _keep(current: Optional[str], value: str) -> str:
        value = (value or "").strip()
        return value if value else (current or "")

    entry.ref_id = _keep(entry.ref_id, ref_id) if can_edit_ref_id(user) else entry.ref_id
    entry.etd = _keep(entry.etd, etd)
    entry.eta = _keep(entry.eta, eta)
    entry.vessel = _keep(entry.vessel, vessel)
    entry.carrier = _keep(entry.carrier, carrier)
    entry.container_type = _keep(entry.container_type, container_type)
    if (package or "").strip():
        try:
            entry.package = float(package)
        except (TypeError, ValueError):
            pass
    if (cbm or "").strip():
        try:
            entry.cbm = float(cbm)
        except (TypeError, ValueError):
            pass
    if entry.etd and not entry.eta:
        derived = eta_for(entry.mode or "", parse_date(entry.etd))
        if derived:
            entry.eta = derived.isoformat()
    entry.updated_at = datetime.utcnow()
    db.commit()
    try:
        rebuild_shipment_month(db, entry.month or "")
    except Exception:
        logger.exception("刷新装柜列表 Excel 失败")
    audit(db, user, "shipment.info", f"Shipment info updated {entry.custom_no or entry.id}")
    return JSONResponse({"ok": True, "message": "Saved to Shipment List"})


@router.post("/api/invoices/manual-save")
async def invoice_manual_save(
    body: ExportBody,
    ids: str = "",
    user: User = Depends(require_type1_or_2),
    db: Session = Depends(get_db),
) -> object:
    """保存编辑页人工字段（Ctrl+S），不生成文件。带并发冲突检测。"""
    invoices = _load_invoices(db, _parse_ids(ids))
    if not invoices:
        return JSONResponse({"ok": False, "error": "Invoice not found"}, status_code=404)

    server_saved = next(
        (i.manual_saved_at for i in invoices if i.manual_saved_at), None
    )
    if not body.force and server_saved is not None and body.last_saved_at:
        try:
            client_ts = datetime.fromisoformat(body.last_saved_at)
        except ValueError:
            client_ts = None
        if client_ts is not None and server_saved > client_ts + timedelta(seconds=1):
            who = next((i.manual_saved_by for i in invoices if i.manual_saved_by), "-")
            return JSONResponse(
                {
                    "ok": True,
                    "conflict": True,
                    "saved_by": who,
                    "saved_at": server_saved.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )

    values, _ = validate_form(body.form)
    save_manual_values(db, invoices, values, user)
    record_manual_save(db, invoices, values, user)
    audit(
        db,
        user,
        "manual.save",
        f"Saved manual fields {', '.join(i.invoice_no or str(i.id) for i in invoices)}",
    )
    now = datetime.utcnow()
    return JSONResponse(
        {"ok": True, "saved": len(values), "saved_at": now.strftime("%Y-%m-%d %H:%M:%S")}
    )


@router.get("/api/invoices/reset-plan")
async def invoice_reset_plan(
    ids: str = "",
    user: User = Depends(require_type1_or_2),
    db: Session = Depends(get_db),
) -> object:
    """一键重置计划（清空 / 恢复上个版本），供前端确认弹窗展示，不落库。"""
    invoices = _load_invoices(db, _parse_ids(ids))
    if not invoices:
        return JSONResponse({"ok": False, "error": "Invoice not found"}, status_code=404)
    plan = reset_manual_state(db, invoices, user, apply=False)
    plan["ok"] = True
    return JSONResponse(plan)


@router.post("/api/invoices/reset")
async def invoice_reset(
    ids: str = "",
    user: User = Depends(require_type1_or_2),
    db: Session = Depends(get_db),
) -> object:
    """一键重置编辑页填写内容：一次生成→清空；旧版报关单→恢复成上个版本。"""
    invoices = _load_invoices(db, _parse_ids(ids))
    if not invoices:
        return JSONResponse({"ok": False, "error": "Invoice not found"}, status_code=404)
    result = reset_manual_state(db, invoices, user, apply=True)
    audit(
        db,
        user,
        "manual.reset",
        f"Reset manual fields {', '.join(i.invoice_no or str(i.id) for i in invoices)}"
        f" ({result['mode']}, target version {result['target_version'] or '-'})",
    )
    result["ok"] = True
    result["saved_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    return JSONResponse(result)


@router.get("/api/download")
async def download(
    folder: str = "",
    name: str = "",
    user: User = Depends(require_any),
    db: Session = Depends(get_db),
) -> object:
    from core.config import OUTPUT_DIR

    base = OUTPUT_DIR.resolve()
    target = (base / folder / name).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid path"}, status_code=400)
    if not target.is_file():
        return JSONResponse({"ok": False, "error": "File not found"}, status_code=404)
    # 静默记录下载行为：谁在何时下载了哪份文件（用于甩锅时回溯取证，同事不可见）
    audit(db, user, "download", f"{folder}/{name}")
    return FileResponse(
        target,
        media_type="application/vnd.ms-excel.sheet.macroEnabled.12",
        filename=target.name,
    )


def _locate_output_file(folder: str, name: str) -> Optional[Path]:
    """定位正式输出文件（平台唯一文件）。"""
    from core.config import OUTPUT_DIR

    base = OUTPUT_DIR.resolve()
    target = (base / folder / name).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target if target.is_file() else None


@router.get("/api/files/preview")
async def file_preview(
    folder: str = "",
    name: str = "",
    user: User = Depends(require_any),
) -> object:
    """在线预览导出文件（隐藏追踪表不出现在预览中）。"""
    from openpyxl import load_workbook

    from ..converters.univer_snapshot import workbook_to_snapshot

    target = _locate_output_file(folder, name)
    if target is None:
        return JSONResponse({"ok": False, "error": "File not found"}, status_code=404)
    try:
        wb = load_workbook(str(target), keep_vba=False)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": f"Cannot read file: {exc}"}, status_code=400
        )
    try:
        if "_PLATFORM_TRACE" in wb.sheetnames:
            del wb["_PLATFORM_TRACE"]
        snapshot = workbook_to_snapshot(
            wb, workbook_id=f"preview-{name}", workbook_name=name
        )
    finally:
        wb.close()
    return JSONResponse({"ok": True, "snapshot": snapshot, "name": name})


# ---- 报关发票（平台唯一文件 + 修改记录） ----


@router.get("/files")
async def file_versions(
    request: Request,
    q: str = "",
    page: int = 1,
    user: User = Depends(require_any),
    db: Session = Depends(get_db),
) -> object:
    """报关发票已并入 Shipment List，/files 永久跳转。"""
    return RedirectResponse("/shipments", status_code=303)


@router.get("/api/files/history")
async def file_history(
    folder: str = "",
    name: str = "",
    user: User = Depends(require_any),
    db: Session = Depends(get_db),
) -> object:
    """某文件的修改记录：历次生成 + 编辑保存（按时间线合并）。"""
    exports = (
        db.query(ExportFileVersion)
        .filter(
            ExportFileVersion.kind == "export",
            ExportFileVersion.folder == folder,
            ExportFileVersion.filename == name,
        )
        .order_by(ExportFileVersion.id.asc())
        .all()
    )
    if not exports:
        return JSONResponse({"ok": False, "error": "File record not found"}, status_code=404)

    inv_ids: set[int] = set()
    for row in exports:
        try:
            inv_ids.update(int(i) for i in json.loads(row.invoice_ids or "[]"))
        except (TypeError, ValueError):
            pass

    saves: list[ExportFileVersion] = []
    if inv_ids:
        for s in (
            db.query(ExportFileVersion)
            .filter(ExportFileVersion.kind == "save")
            .order_by(ExportFileVersion.id.desc())
            .limit(2000)
            .all()
        ):
            try:
                sids = {int(i) for i in json.loads(s.invoice_ids or "[]")}
            except (TypeError, ValueError):
                sids = set()
            if sids & inv_ids:
                saves.append(s)

    records: list[dict[str, Any]] = []
    for row in exports:
        records.append(
            {
                "_dt": row.created_at,
                "_rank": 0,
                "_id": row.id,
                "_values": _form_dict(row.form_values),
                "_stored": _stored_changes(row.form_values),
                "time": fmt_local(row.created_at),
                "kind": "export",
                "by": row.created_by or "-",
                "version": row.version,
                "sha": (row.sha256 or "")[:8],
                "backup": f"/api/files/backup?id={row.id}" if row.backup_path else "",
            }
        )
    for row in saves:
        records.append(
            {
                "_dt": row.created_at,
                "_rank": 1,
                "_id": row.id,
                "_values": _form_dict(row.form_values),
                "_stored": _stored_changes(row.form_values),
                "time": fmt_local(row.created_at),
                "kind": "save",
                "by": row.created_by or "-",
                "version": 0,
                # 行内编辑改写 .xlsm 的 save 行带 sha256 与留底，编辑页保存的 trace 行为空
                "sha": (row.sha256 or "")[:8],
                "backup": f"/api/files/backup?id={row.id}" if row.backup_path else "",
            }
        )

    records.sort(key=lambda r: (r["_dt"], r["_rank"], r["_id"]))
    timeline: list[dict[str, Any]] = []
    previous: dict[str, str] = {}
    for rec in records:
        values = rec.pop("_values")
        stored = rec.pop("_stored")
        for key in ("_dt", "_rank", "_id"):
            rec.pop(key, None)
        if stored:
            # 行内编辑记录里带精确的 old→new，直接展示
            changes = stored
        else:
            # 缺键 = 该记录未触碰此字段（沿用上一份值），空值 = 显式清空
            changes = []
            for key, new in values.items():
                old = previous.get(key, "")
                if old != new:
                    changes.append(
                        {
                            "key": key,
                            "label": FORM_LABELS.get(key, key),
                            "old": old,
                            "new": new,
                        }
                    )
        rec["changes"] = changes
        rec["form"] = _form_text(values)
        timeline.append(rec)
        previous.update(values)

    return JSONResponse({"ok": True, "folder": folder, "name": name, "rows": timeline})


FORM_LABELS = {
    "date": "Date",
    "bill_no": "Invoice No.",
    "supplier": "Supplier",
    "ship_mode": "Ship Mode",
    "place_receipt": "Place of Receipt",
    "port_loading": "Port of Loading",
    "container_info": "Container / Seal No.",
    "flight_no": "Flight No.",
    "unit_prices": "Unit Price Overrides",
    # Shipment List 行内编辑字段
    "package": "Package",
    "nw_kg": "NW KG",
    "gw_kg": "GW KG",
    "etd": "ETD",
    "eta": "ETA",
    "vessel": "VESSEL",
    "carrier": "Carrier",
    "container_type": "Container Type",
    "cbm": "CBM",
    "fty_inv": "Fty Inv",
    "shipper": "Shipper",
    "est_vco": "EST/VCO",
    "custom_no": "Custom #",
    "blading": "Blading",
    "description": "DESCRIPTION",
    "ref_id": "ID",
}


def _form_dict(raw: Any) -> dict[str, str]:
    """把 form_values JSON 解析成 {字段: 文本值}（下划线开头的内部键跳过）。"""
    try:
        data = json.loads(raw or "{}") if isinstance(raw, str) else (raw or {})
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(key): ("" if value is None else str(value)).strip()
        for key, value in data.items()
        if not str(key).startswith("_")
    }


def _stored_changes(raw: Any) -> list[dict[str, str]]:
    """行内编辑在 form_values["_changes"] 里直接记录的字段变更，无则空。"""
    try:
        data = json.loads(raw or "{}") if isinstance(raw, str) else (raw or {})
    except (TypeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    changes = data.get("_changes")
    if not isinstance(changes, list):
        return []
    return [
        {
            "key": str(c.get("key", "")),
            "label": str(c.get("label", "")) or str(c.get("key", "")),
            "old": str(c.get("old", "")),
            "new": str(c.get("new", "")),
        }
        for c in changes
        if isinstance(c, dict)
    ]


def _form_text(values: dict[str, str]) -> str:
    """完整字段快照文本（供“查看完整字段”展示）。"""
    return "；".join(
        f"{FORM_LABELS.get(key, key)}: {value}"
        for key, value in values.items()
        if value
    )


def _safe_form_values(raw: str) -> str:
    return _form_text(_form_dict(raw))


@router.get("/api/files/backup")
async def file_backup(
    id: int = 0,
    user: User = Depends(require_any),
    db: Session = Depends(get_db),
) -> object:
    """下载某次生成前的留底文件（内部存档，按记录号定位）。"""
    from core.config import HISTORY_DIR

    record = db.get(ExportFileVersion, id)
    if record is None or not record.backup_path:
        return JSONResponse({"ok": False, "error": "Backup file not found"}, status_code=404)
    base = HISTORY_DIR.resolve()
    target = Path(record.backup_path).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid path"}, status_code=400)
    if not target.is_file():
        return JSONResponse({"ok": False, "error": "Backup file not found"}, status_code=404)
    return FileResponse(
        target,
        media_type="application/vnd.ms-excel.sheet.macroEnabled.12",
        filename=target.name,
    )


def _original_file_for(db: Session, record: ExportFileVersion) -> Optional[Path]:
    """返回该版本导出内容的真实文件路径。

    平台同名文件唯一：重新生成会覆盖正式文件，旧版本的实际内容存在
    「下一版记录」的 backup_path（生成下一版时自动归档的留底）里。
    只有最新版的 file_path 才是该版本自己的内容。
    """
    nxt = (
        db.query(ExportFileVersion)
        .filter(
            ExportFileVersion.kind == "export",
            ExportFileVersion.folder == record.folder,
            ExportFileVersion.filename == record.filename,
            ExportFileVersion.version == record.version + 1,
        )
        .first()
    )
    if nxt and nxt.backup_path:
        path = Path(nxt.backup_path)
        if path.is_file():
            return path
        # 有下一版但留底丢失：旧内容已不可考，不能拿最新版冒充
        return None
    if record.file_path:
        path = Path(record.file_path)
        if path.is_file():
            return path
    return None


def _closest_version(
    db: Session, filename: str, data: bytes
) -> Optional[tuple[Path, dict]]:
    """按文件名找全部历史版本文件，返回改动最少的一份 (路径, 来源信息)。"""
    from core.compare import diff_xlsm

    records = (
        db.query(ExportFileVersion)
        .filter(
            ExportFileVersion.kind == "export",
            ExportFileVersion.filename == filename,
        )
        .order_by(ExportFileVersion.id.desc())
        .all()
    )
    best_path: Optional[Path] = None
    best_source: dict = {}
    best_count: Optional[int] = None
    seen: set[str] = set()
    for rec in records:
        path = _original_file_for(db, rec)
        if path is None or str(path) in seen:
            continue
        seen.add(str(path))
        try:
            result = diff_xlsm(path, data)
        except Exception:  # noqa: BLE001
            continue
        if best_count is None or result["count"] < best_count:
            best_count = result["count"]
            best_path = path
            best_source = {
                "folder": rec.folder,
                "filename": rec.filename,
                "version": rec.version,
                "exported_by": rec.created_by or "-",
            }
    if best_path is None:
        return None
    return best_path, best_source


def _diff_against_original(
    db: Session, data: bytes, filename_hint: str
) -> Optional[dict]:
    """定位回传文件对应的原始导出并逐格比对；无法定位返回 None。"""
    from core.compare import diff_xlsm, read_doc_props
    from core.exporter import read_trace_sheet

    # 1) 从内嵌追踪表读 JOB_ID / FILE_STEM，定位原始导出记录
    trace: dict[str, str] = {}
    with tempfile.NamedTemporaryFile(suffix=".xlsm", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        tmp.write(data)
    try:
        trace = read_trace_sheet(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    record: Optional[ExportFileVersion] = None
    job_id = trace.get("JOB_ID", "").strip()
    if job_id.isdigit():
        record = (
            db.query(ExportFileVersion)
            .filter(
                ExportFileVersion.kind == "export",
                ExportFileVersion.job_id == int(job_id),
            )
            .order_by(ExportFileVersion.id.desc())
            .first()
        )

    original: Optional[Path] = None
    source: dict = {}
    if record is not None:
        original = _original_file_for(db, record)
        source = {
            "folder": record.folder,
            "filename": record.filename,
            "version": record.version,
            "exported_by": record.created_by or "-",
        }

    # 2) 追踪表缺失/失效：按文件名兜底，比对全部历史版本取改动最少者
    if original is None:
        filename = filename_hint
        stem = trace.get("FILE_STEM", "").strip()
        if stem:
            filename = stem + ".xlsm"
        if not filename:
            return None
        best = _closest_version(db, filename, data)
        if best is None:
            return None
        original, source = best

    try:
        result = diff_xlsm(original, data)
    except Exception as exc:  # noqa: BLE001
        return {
            "source": source,
            "trace_missing": not bool(trace),
            "diff_error": f"Content comparison failed: {exc}",
            "original_props": read_doc_props(original.read_bytes()),
        }

    return {
        "source": source,
        "trace_missing": not bool(trace),
        "diff": result["changes"][:300],
        "diff_count": result["count"],
        "original_props": read_doc_props(original.read_bytes()),
    }


@router.post("/api/files/verify")
async def file_verify(
    file: UploadFile = File(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> object:
    """把文件传回来校验：与平台导出记录的 SHA256 比对，判断是否被本地改动。

    仅管理员可用：改动详情、修改人等取证信息不对普通账号开放。
    """
    data = await file.read()
    if not data:
        return JSONResponse({"ok": False, "error": "Empty file"}, status_code=400)
    sha = hashlib.sha256(data).hexdigest()

    matches = (
        db.query(ExportFileVersion)
        .filter(
            # 生成记录与带 sha256 的编辑保存记录都参与比对
            # （平台行内改写 .xlsm 后的新文件内容对应 save 行，避免误报）
            ExportFileVersion.sha256 == sha,
            ExportFileVersion.sha256 != "",
        )
        .order_by(ExportFileVersion.id.desc())
        .all()
    )
    if not matches:
        from core.compare import read_doc_props

        payload: dict = {
            "ok": True,
            "matched": False,
            "sha": sha,
            "message": "Matches no platform export record: this file was modified (or re-saved) after export.",
            # Excel 保存时自动记录的保存人/保存时间——谁改的、何时改的旁证
            "uploaded_props": read_doc_props(data),
        }
        diff_info = _diff_against_original(db, data, file.filename or "")
        if diff_info is not None:
            payload.update(diff_info)
        return JSONResponse(payload)
    m = matches[0]
    if m.kind == "export":
        message = (
            f"Matches platform record: {m.folder}/{m.filename}, generation #{m.version}, "
            f"exported by {m.created_by or '-'}, at {fmt_local(m.created_at)}."
        )
    else:
        message = (
            f"Matches platform record: {m.folder}/{m.filename}, platform edit-save version, "
            f"operator {m.created_by or '-'}, at {fmt_local(m.created_at)}."
        )
    return JSONResponse(
        {
            "ok": True,
            "matched": True,
            "sha": sha,
            "folder": m.folder,
            "filename": m.filename,
            "version": m.version,
            "exported_by": m.created_by or "-",
            "exported_at": fmt_local(m.created_at),
            "message": message,
        }
    )
