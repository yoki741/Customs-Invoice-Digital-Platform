# -*- coding: utf-8 -*-
"""第一类账号：发票列表、Excel 编辑页、导出、文件版本。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session, noload

from ..auth import audit, require_any, require_type1
from ..converters.field_map import EDITABLE_FIELDS, EXTRA_FIELDS, validate_form
from ..models import (
    CHECK_FAILED,
    CHECK_PENDING,
    CHECK_PASSED,
    ExportFileVersion,
    Invoice,
    ShipmentListEntry,
    User,
    get_db,
)
from ..service import (
    build_for_edit,
    build_snapshot,
    export_invoices,
    record_failure,
    record_manual_save,
    save_manual_values,
)
from ..templates_setup import templates

router = APIRouter(tags=["invoices"])


class ExportBody(BaseModel):
    form: dict = {}
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
            "check_status": check_status,
            "date_from": date_from,
            "date_to": date_to,
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
    back: str = "",
    user: User = Depends(require_type1),
    db: Session = Depends(get_db),
) -> object:
    # 返回列表链接（还原筛选条件），只允许 ?开头的安全查询串
    back_url = back if back.startswith("?") and len(back) < 500 else ""
    back_link = f"/invoices{back_url}" if back_url else "/invoices"
    id_list = _parse_ids(ids)
    invoices = _load_invoices(db, id_list)
    if not invoices:
        return templates.TemplateResponse(
            request,
            "message.html",
            {"user": user, "level": "warning", "title": "无法编辑",
             "message": "未找到选中的发票", "back": back_link},
            status_code=404,
        )

    # 页面只做轻量校验（收货方一致性），真正的构建交给快照接口（带缓存），
    # 避免每次打开编辑页重复解析源文件、构建工作簿
    deliveries = {i.delivery_to or "UNKNOWN" for i in invoices}
    if len(deliveries) > 1:
        return templates.TemplateResponse(
            request,
            "message.html",
            {"user": user, "level": "warning", "title": "无法编辑",
             "message": "选中的发票 Delivery to 不一致，无法合并导出："
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


@router.get("/shipments")
async def shipment_list_page(
    request: Request,
    month: str = "",
    company: str = "",
    mode: str = "",
    q: str = "",
    page: int = 1,
    user: User = Depends(require_type1),
    db: Session = Depends(get_db),
) -> object:
    """装柜列表（Shipment List），第一类账号与管理员可见。"""
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
    user: User = Depends(require_type1),
) -> object:
    """下载某月的装柜列表 Excel。"""
    import re as _re

    from core.shipment_list import month_path

    if not _re.fullmatch(r"\d{4}-\d{2}", month):
        return JSONResponse({"ok": False, "error": "月份格式应为 YYYY-MM"}, status_code=400)
    path = month_path(month)
    if not path.is_file():
        return JSONResponse({"ok": False, "error": "该月份的装柜列表不存在"}, status_code=404)
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
    user: User = Depends(require_type1),
    db: Session = Depends(get_db),
) -> object:
    """保存装柜列表某行手动填写的 ID（仅存数据库，不写入 Excel）。"""
    from urllib.parse import urlencode

    entry = db.get(ShipmentListEntry, entry_id)
    if entry is None:
        return JSONResponse({"ok": False, "error": "记录不存在"}, status_code=404)

    entry.ref_id = (ref_id or "").strip()
    entry.updated_at = datetime.utcnow()
    db.commit()
    audit(
        db,
        user,
        "shipment.ref_id",
        f"装柜 ID {entry.custom_no or entry_id} → {entry.ref_id or '(空)'}",
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


@router.post("/api/invoices/manual-save")
async def invoice_manual_save(
    body: ExportBody,
    ids: str = "",
    user: User = Depends(require_type1),
    db: Session = Depends(get_db),
) -> object:
    """保存编辑页人工字段（Ctrl+S），不生成文件。带并发冲突检测。"""
    invoices = _load_invoices(db, _parse_ids(ids))
    if not invoices:
        return JSONResponse({"ok": False, "error": "未找到发票"}, status_code=404)

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
        f"保存人工字段 {', '.join(i.invoice_no or str(i.id) for i in invoices)}",
    )
    now = datetime.utcnow()
    return JSONResponse(
        {"ok": True, "saved": len(values), "saved_at": now.strftime("%Y-%m-%d %H:%M:%S")}
    )


@router.get("/api/download")
async def download(
    folder: str = "",
    name: str = "",
    user: User = Depends(require_any),
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
        return JSONResponse({"ok": False, "error": "文件不存在"}, status_code=404)
    try:
        wb = load_workbook(str(target), keep_vba=False)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": f"无法读取文件: {exc}"}, status_code=400
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

KIND_LABELS = {"export": "生成文件", "save": "编辑保存"}


@router.get("/files")
async def file_versions(
    request: Request,
    q: str = "",
    page: int = 1,
    user: User = Depends(require_any),
    db: Session = Depends(get_db),
) -> object:
    from sqlalchemy import func

    filters = [ExportFileVersion.kind == "export"]
    if q.strip():
        like = f"%{q.strip()}%"
        filters.append(
            or_(
                ExportFileVersion.filename.ilike(like),
                ExportFileVersion.folder.ilike(like),
                ExportFileVersion.created_by.ilike(like),
            )
        )
    # SQL 分组：按 folder+filename 取最新一条，避免全量加载到内存再分组
    grouped = (
        db.query(
            ExportFileVersion.folder,
            ExportFileVersion.filename,
            func.count(ExportFileVersion.id).label("cnt"),
            func.max(ExportFileVersion.id).label("mid"),
        )
        .filter(*filters)
        .group_by(ExportFileVersion.folder, ExportFileVersion.filename)
        .subquery()
    )
    total = db.query(func.count()).select_from(grouped).scalar()
    page = max(page, 1)
    size = 30
    row_keys = (
        db.query(
            grouped.c.folder, grouped.c.filename, grouped.c.cnt, grouped.c.mid
        )
        .order_by(grouped.c.mid.desc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    pages = max((total + size - 1) // size, 1)
    mids = [r.mid for r in row_keys]
    rec_map = {
        r.id: r
        for r in db.query(ExportFileVersion)
        .filter(ExportFileVersion.id.in_(mids))
        .all()
    }
    rows = [
        {
            "folder": r.folder,
            "filename": r.filename,
            "count": r.cnt,
            "latest": rec_map[r.mid],
        }
        for r in row_keys
        if r.mid in rec_map
    ]
    return templates.TemplateResponse(
        request,
        "files.html",
        {
            "user": user,
            "rows": rows,
            "kind_labels": KIND_LABELS,
            "q": q,
            "page": page,
            "pages": pages,
            "total": total,
            "flash": request.session.pop("flash", None),
        },
    )


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
        return JSONResponse({"ok": False, "error": "文件记录不存在"}, status_code=404)

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
                "time": row.created_at.strftime("%Y-%m-%d %H:%M:%S"),
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
                "time": row.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                "kind": "save",
                "by": row.created_by or "-",
                "version": 0,
                "sha": "",
                "backup": "",
            }
        )

    records.sort(key=lambda r: (r["_dt"], r["_rank"], r["_id"]))
    timeline: list[dict[str, Any]] = []
    previous: dict[str, str] = {}
    for rec in records:
        values = rec.pop("_values")
        for key in ("_dt", "_rank", "_id"):
            rec.pop(key, None)
        keys = list(previous.keys()) + [k for k in values if k not in previous]
        changes = []
        for key in keys:
            old, new = previous.get(key, ""), values.get(key, "")
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
        previous = values

    return JSONResponse({"ok": True, "folder": folder, "name": name, "rows": timeline})


FORM_LABELS = {
    "date": "日期",
    "bill_no": "发票号",
    "supplier": "供应商",
    "ship_mode": "运输方式",
    "place_receipt": "起运地/装货地",
    "port_loading": "装货港",
    "container_info": "柜号/封条",
    "flight_no": "航班号",
    "unit_prices": "单价覆盖",
}


def _form_dict(raw: Any) -> dict[str, str]:
    """把 form_values JSON 解析成 {字段: 文本值}。"""
    try:
        data = json.loads(raw or "{}") if isinstance(raw, str) else (raw or {})
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(key): ("" if value is None else str(value)).strip()
        for key, value in data.items()
    }


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
        return JSONResponse({"ok": False, "error": "留底文件不存在"}, status_code=404)
    base = HISTORY_DIR.resolve()
    target = Path(record.backup_path).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return JSONResponse({"ok": False, "error": "非法路径"}, status_code=400)
    if not target.is_file():
        return JSONResponse({"ok": False, "error": "留底文件不存在"}, status_code=404)
    return FileResponse(
        target,
        media_type="application/vnd.ms-excel.sheet.macroEnabled.12",
        filename=target.name,
    )
