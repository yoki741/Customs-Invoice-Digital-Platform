# -*- coding: utf-8 -*-
"""管理员：账号、数据导入、导出历史。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session, noload

from ..auth import ROLE_ALL, audit, hash_password, require_admin, require_csrf, require_type1
from ..timefmt import fmt_local
from ..importer import import_source_file, save_upload, scan_uploads
from ..models import ROLE_LABELS, AuditLog, ExportJob, Invoice, User, get_db
from ..templates_setup import templates
from .invoice_router import invoice_list_context

router = APIRouter(prefix="/admin", tags=["admin"])


def _flash(request: Request, level: str, message: str) -> None:
    request.session["flash"] = {"level": level, "message": message}


def _friendly_import_error(exc: Exception) -> str:
    """把解析层的英文/技术错误翻译成用户看得懂的提示。"""
    text = str(exc)
    lowered = text.lower()
    if "does not support" in lowered and "file format" in lowered:
        return "Unsupported file format, please upload an .xlsm / .xlsx Excel file"
    if "is not a zip file" in lowered or "cannot open zlib" in lowered:
        return "The file is corrupted or not a valid Excel file, please check and upload again"
    if "InputTable worksheet" in text:
        return text.replace("'", "").strip("'")
    if "Invoice No" in text:
        return text.replace("'", "").strip("'")
    if "PermissionError" in type(exc).__name__ or "permission" in lowered:
        return "File access was rejected, it may be open in another program, please close it and try again"
    # 其余情况截断长错误，避免整段堆栈吓到用户
    return text[:200]


# ---- 首页 ----


@router.get("")
async def admin_home(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> object:
    stats = {
        "invoices": db.query(Invoice).count(),
        "users": db.query(User).count(),
        "active_users": db.query(User).filter(User.is_active.is_(True)).count(),
        "exports": db.query(ExportJob).filter(ExportJob.success.is_(True)).count(),
        "failed": db.query(ExportJob).filter(ExportJob.success.is_(False)).count(),
    }
    recent = (
        db.query(Invoice)
        .options(noload(Invoice.items))
        .order_by(Invoice.uploaded_at.desc())
        .limit(8)
        .all()
    )
    return templates.TemplateResponse(
        request,
        "admin_home.html",
        {"user": user, "stats": stats, "recent": recent,
         "flash": request.session.pop("flash", None)},
    )


# ---- 账号管理 ----


@router.get("/users")
async def user_list(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> object:
    rows = db.query(User).order_by(User.id.asc()).all()
    return templates.TemplateResponse(
        request,
        "admin_users.html",
        {"user": user, "rows": rows, "role_labels": ROLE_LABELS,
         "error": request.session.pop("flash_error", ""),
         "flash": request.session.pop("flash", None)},
    )


@router.post("/users")
async def user_create(
    request: Request,
    csrf: str = Form(""),
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    display_name: str = Form(""),
    dept: str = Form(""),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> object:
    require_csrf(request, csrf)
    name = username.strip()
    if not name or len(password) < 4:
        _flash(request, "warning", "Username cannot be empty and the password must be at least 4 characters")
        return RedirectResponse("/admin/users", status_code=303)
    if role not in ROLE_ALL:
        _flash(request, "warning", "Invalid role")
        return RedirectResponse("/admin/users", status_code=303)
    if db.query(User).filter(User.username == name).one_or_none():
        _flash(request, "warning", f"Account {name} already exists")
        return RedirectResponse("/admin/users", status_code=303)

    db.add(
        User(
            username=name,
            password_hash=hash_password(password),
            role=role,
            display_name=display_name.strip() or name,
            dept=dept.strip(),
        )
    )
    db.commit()
    audit(db, user, "user.create", f"Created account {name} ({role})")
    _flash(request, "success", f"Account {name} created")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/role")
async def user_role(
    request: Request,
    user_id: int,
    role: str = Form(...),
    csrf: str = Form(""),
    current: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> object:
    require_csrf(request, csrf)
    target = db.get(User, user_id)
    if target and role in ROLE_ALL:
        old = target.role
        target.role = role
        db.commit()
        audit(db, current, "user.role", f"{target.username}: {old} -> {role}")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/toggle")
async def user_toggle(
    request: Request,
    user_id: int,
    csrf: str = Form(""),
    current: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> object:
    require_csrf(request, csrf)
    target = db.get(User, user_id)
    if target and target.id != current.id:
        target.is_active = not target.is_active
        db.commit()
        audit(db, current, "user.toggle",
              f"{target.username} -> {'enabled' if target.is_active else 'disabled'}")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/password")
async def user_password(
    request: Request,
    user_id: int,
    csrf: str = Form(""),
    password: str = Form(...),
    current: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> object:
    require_csrf(request, csrf)
    target = db.get(User, user_id)
    if target and len(password) >= 4:
        target.password_hash = hash_password(password)
        db.commit()
        audit(db, current, "user.password", f"Reset password for {target.username}")
        _flash(request, "success", f"Password for {target.username} has been reset")
    else:
        _flash(request, "warning", "Password must be at least 4 characters")
    return RedirectResponse("/admin/users", status_code=303)


# ---- 数据导入（已并入发票列表页，此处仅保留处理入口） ----


@router.get("/upload")
async def upload_page(
    request: Request,
    user: User = Depends(require_type1),
) -> object:
    """旧「数据导入」页地址，跳转到发票列表（导入已是列表页内的功能）。"""
    return RedirectResponse("/invoices", status_code=303)


@router.post("/upload")
async def upload_files(
    request: Request,
    csrf: str = Form(""),
    confirm: str = Form(""),
    pending_json: str = Form(""),
    files: list[UploadFile] = File(default=[]),
    user: User = Depends(require_type1),
    db: Session = Depends(get_db),
) -> object:
    """数据导入处理，结果直接渲染在发票列表页（含重复确认 / 导入结果）。"""
    require_csrf(request, csrf)

    def render(results: list[dict], pending: list[dict], highlight_ids: list[int]) -> object:
        return templates.TemplateResponse(
            request,
            "invoices.html",
            invoice_list_context(
                request, db, user,
                pending=pending, results=results,
                highlight_ids=highlight_ids,
            ),
        )

    # 第二段：用户在确认表单里点了「确认导入」
    if confirm == "1":
        results = []
        highlight_ids: list[int] = []
        for entry in json.loads(pending_json or "[]"):
            if not entry.get("overwrite"):
                results.append({"file": entry["file"], "ok": False,
                                "invoice_no": entry.get("invoice_no", "-"),
                                "message": "Skipped (overwrite not checked)"})
                continue
            path = Path(entry["path"])
            if not path.exists():
                results.append({"file": entry["file"], "ok": False,
                                "invoice_no": entry.get("invoice_no", "-"),
                                "message": "Source file not found, please upload again"})
                continue
            try:
                invoice, created = import_source_file(db, path, user)
                highlight_ids.append(invoice.id)
                results.append(
                    {"file": entry["file"], "ok": True,
                     "invoice_no": invoice.invoice_no,
                     "message": "New" if created else "Overwrote existing invoice"}
                )
                audit(db, user, "import", f"{entry['file']} -> {invoice.invoice_no}")
            except Exception as exc:
                db.rollback()
                results.append({"file": entry["file"], "ok": False,
                                "invoice_no": entry.get("invoice_no", "-"),
                                "message": _friendly_import_error(exc)})
        ok = sum(1 for r in results if r["ok"])
        audit(db, user, "import.batch", f"Confirmed import of {len(results)} file(s), {ok} succeeded")
        return render(results, [], highlight_ids)

    # 第一段：解析 + 检测重复，无重复的直接入库
    results: list[dict] = []
    pending: list[dict] = []
    highlight_ids = []
    for upload in files:
        if not upload.filename:
            continue
        raw = await upload.read()
        if not raw:
            results.append({"file": upload.filename, "ok": False, "message": "Empty file"})
            continue
        # 大小预检：超过 20MB 拒绝，防滥用
        if len(raw) > 20 * 1024 * 1024:
            results.append({
                "file": upload.filename, "ok": False,
                "message": "File exceeds 20MB, please check the file content and try again",
            })
            continue
        # 扩展名预检：不合法直接提示，不落盘
        suffix = Path(upload.filename).suffix.lower()
        if suffix not in (".xlsm", ".xlsx"):
            results.append({
                "file": upload.filename, "ok": False,
                "message": f"Unsupported format: {suffix or '(no extension)'}, only .xlsm / .xlsx Excel files are accepted",
            })
            continue
        try:
            path = save_upload(upload.filename, raw)
            scan = scan_uploads(db, path)
        except Exception as exc:
            results.append({
                "file": upload.filename, "ok": False,
                "message": _friendly_import_error(exc),
            })
            continue
        if scan["exists"]:
            ex = scan["existing"] or {}
            pending.append({
                "file": upload.filename,
                "path": str(path),
                "invoice_no": scan["invoice_no"],
                "uploaded_at": fmt_local(ex.get("uploaded_at"), "%Y-%m-%d %H:%M")
                if ex.get("uploaded_at") else "-",
                "uploaded_by": ex.get("uploaded_by") or "-",
                "source_filename": ex.get("source_filename") or "-",
            })
        else:
            try:
                invoice, created = import_source_file(db, path, user)
                highlight_ids.append(invoice.id)
                results.append(
                    {"file": upload.filename, "ok": True,
                     "invoice_no": invoice.invoice_no,
                     "message": "New" if created else "Replaced existing invoice"}
                )
                audit(db, user, "import", f"{upload.filename} -> {invoice.invoice_no}")
            except Exception as exc:
                db.rollback()
                results.append({"file": upload.filename, "ok": False,
                                "message": _friendly_import_error(exc)})

    ok = sum(1 for r in results if r["ok"])
    if results:
        audit(db, user, "import.batch", f"Uploaded {len(results)} file(s), {ok} succeeded")
    return render(results, pending, highlight_ids)


# ---- 导出历史 ----


@router.get("/exports")
async def export_history(
    request: Request,
    page: int = 1,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> object:
    query = db.query(ExportJob)
    total = query.count()
    page = max(page, 1)
    size = 30
    rows = (
        query.order_by(ExportJob.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    all_ids: set[int] = set()
    for row in rows:
        try:
            all_ids.update(int(i) for i in json.loads(row.invoice_ids))
        except Exception:
            pass
    invoice_map = (
        {i.id: i.invoice_no for i in db.query(Invoice).options(noload(Invoice.items)).filter(Invoice.id.in_(all_ids))}
        if all_ids
        else {}
    )
    for row in rows:
        try:
            ids = json.loads(row.invoice_ids)
        except Exception:
            ids = []
        row.invoice_nos = ", ".join(
            str(invoice_map.get(i, i)) for i in ids
        )
    pages = max((total + size - 1) // size, 1)
    return templates.TemplateResponse(
        request,
        "admin_exports.html",
        {"user": user, "rows": rows, "page": page, "pages": pages, "total": total},
    )


# ---- 审计日志 ----

@router.get("/audit")
async def audit_log(
    request: Request,
    q: str = "",
    action: str = "",
    page: int = 1,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> object:
    query = db.query(AuditLog)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(
            AuditLog.username.ilike(like) | AuditLog.detail.ilike(like)
        )
    if action.strip():
        query = query.filter(AuditLog.action == action.strip())

    total = query.count()
    page = max(page, 1)
    size = 50
    rows = (
        query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    pages = max((total + size - 1) // size, 1)
    actions = [
        a
        for (a,) in db.query(AuditLog.action).distinct().order_by(AuditLog.action)
    ]
    return templates.TemplateResponse(
        request,
        "admin_audit.html",
        {
            "user": user,
            "rows": rows,
            "q": q,
            "action": action,
            "actions": actions,
            "page": page,
            "pages": pages,
            "total": total,
        },
    )


# ---- 发票删除 ----

@router.post("/invoices/{invoice_id}/delete")
async def invoice_delete(
    request: Request,
    invoice_id: int,
    csrf: str = Form(""),
    back: str = Form(""),
    current: User = Depends(require_type1),
    db: Session = Depends(get_db),
) -> object:
    """删除发票记录（第一类账号/管理员；明细级联删除，源文件保留，操作留痕）。"""
    require_csrf(request, csrf)
    invoice = db.get(Invoice, invoice_id)
    target = back or "/invoices"
    if not target.startswith("/"):
        target = "/invoices"
    if invoice is None:
        _flash(request, "warning", "Invoice not found, it may have been deleted")
        return RedirectResponse(target, status_code=303)
    no = invoice.invoice_no or f"#{invoice.id}"
    source = invoice.source_filename or "-"
    db.delete(invoice)
    db.commit()
    audit(db, current, "invoice.delete", f"Deleted invoice {no} (source file kept: {source})")
    _flash(request, "success", f"Invoice {no} deleted (source file kept, not deleted)")
    return RedirectResponse(target, status_code=303)


# ---- 统计报表 ----

@router.get("/stats")
async def admin_stats(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> object:
    from sqlalchemy import func

    # 按月导出统计（最近 12 个月）
    monthly_rows = (
        db.query(
            func.strftime("%Y-%m", ExportJob.created_at).label("ym"),
            func.count(ExportJob.id).label("cnt"),
            func.sum(ExportJob.customs_weight).label("w"),
            func.sum(ExportJob.total_amount).label("amt"),
        )
        .filter(ExportJob.success.is_(True))
        .group_by("ym")
        .order_by(func.strftime("%Y-%m", ExportJob.created_at).desc())
        .limit(12)
        .all()
    )
    monthly = [
        {
            "ym": r.ym,
            "cnt": r.cnt,
            "weight": int(r.w or 0),
            "amount": round(r.amt or 0, 2),
        }
        for r in monthly_rows
    ]
    monthly.reverse()

    # 台账分布
    by_company = (
        db.query(Invoice.company, func.count(Invoice.id))
        .group_by(Invoice.company)
        .all()
    )
    by_transport = (
        db.query(Invoice.transport, func.count(Invoice.id))
        .group_by(Invoice.transport)
        .all()
    )
    stats = {
        "invoices": db.query(Invoice).count(),
        "exports": db.query(ExportJob).filter(ExportJob.success.is_(True)).count(),
        "failed": db.query(ExportJob).filter(ExportJob.success.is_(False)).count(),
        "companies": [{"name": c or "Unrecognized", "cnt": n} for c, n in by_company],
        "transports": [{"name": t or "Unrecognized", "cnt": n} for t, n in by_transport],
    }
    max_cnt = max((r["cnt"] for r in monthly), default=1)
    for r in monthly:
        r["bar"] = round(r["cnt"] / max_cnt * 100, 1)
    return templates.TemplateResponse(
        request,
        "admin_stats.html",
        {"user": user, "monthly": monthly, "stats": stats},
    )
