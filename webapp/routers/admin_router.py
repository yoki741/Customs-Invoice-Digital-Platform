# -*- coding: utf-8 -*-
"""管理员：账号、数据导入、导出历史。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..auth import ROLE_ALL, audit, hash_password, require_admin, require_csrf
from ..importer import import_source_file, save_upload, scan_uploads
from ..models import ROLE_LABELS, ExportJob, Invoice, User, get_db
from ..templates_setup import templates

router = APIRouter(prefix="/admin", tags=["admin"])


def _flash(request: Request, level: str, message: str) -> None:
    request.session["flash"] = {"level": level, "message": message}


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
        db.query(Invoice).order_by(Invoice.uploaded_at.desc()).limit(8).all()
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
        _flash(request, "warning", "用户名不能为空，密码至少 4 位")
        return RedirectResponse("/admin/users", status_code=303)
    if role not in ROLE_ALL:
        _flash(request, "warning", "角色不合法")
        return RedirectResponse("/admin/users", status_code=303)
    if db.query(User).filter(User.username == name).one_or_none():
        _flash(request, "warning", f"账号 {name} 已存在")
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
    audit(db, user, "user.create", f"创建账号 {name}（{role}）")
    _flash(request, "success", f"账号 {name} 创建成功")
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
              f"{target.username} -> {'启用' if target.is_active else '停用'}")
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
        audit(db, current, "user.password", f"重置 {target.username} 密码")
        _flash(request, "success", f"已重置 {target.username} 的密码")
    else:
        _flash(request, "warning", "密码至少 4 位")
    return RedirectResponse("/admin/users", status_code=303)


# ---- 数据导入 ----


@router.get("/upload")
async def upload_page(
    request: Request,
    user: User = Depends(require_admin),
) -> object:
    return templates.TemplateResponse(
        request,
        "admin_upload.html",
        {"user": user, "results": None, "pending": None,
         "flash": request.session.pop("flash", None)},
    )


@router.post("/upload")
async def upload_files(
    request: Request,
    csrf: str = Form(""),
    confirm: str = Form(""),
    pending_json: str = Form(""),
    files: list[UploadFile] = File(default=[]),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> object:
    require_csrf(request, csrf)

    # 第二段：用户在确认页点了「确认导入」
    if confirm == "1":
        results = []
        for entry in json.loads(pending_json or "[]"):
            if not entry.get("overwrite"):
                results.append({"file": entry["file"], "ok": False,
                                "invoice_no": entry.get("invoice_no", "-"),
                                "message": "已跳过（未勾选覆盖）"})
                continue
            path = Path(entry["path"])
            if not path.exists():
                results.append({"file": entry["file"], "ok": False,
                                "invoice_no": entry.get("invoice_no", "-"),
                                "message": "源文件不存在，请重新上传"})
                continue
            try:
                invoice, created = import_source_file(db, path, user)
                results.append(
                    {"file": entry["file"], "ok": True,
                     "invoice_no": invoice.invoice_no,
                     "message": "新建" if created else "覆盖已有发票"}
                )
                audit(db, user, "import", f"{entry['file']} -> {invoice.invoice_no}")
            except Exception as exc:
                db.rollback()
                results.append({"file": entry["file"], "ok": False,
                                "invoice_no": entry.get("invoice_no", "-"),
                                "message": str(exc)})
        ok = sum(1 for r in results if r["ok"])
        audit(db, user, "import.batch", f"确认导入 {len(results)} 个，成功 {ok} 个")
        return templates.TemplateResponse(
            request,
            "admin_upload.html",
            {"user": user, "results": results, "pending": None,
             "flash": request.session.pop("flash", None)},
        )

    # 第一段：解析 + 检测重复，不入库
    results: list[dict] = []
    pending: list[dict] = []
    for upload in files:
        if not upload.filename:
            continue
        raw = await upload.read()
        if not raw:
            results.append({"file": upload.filename, "ok": False, "message": "空文件"})
            continue
        try:
            path = save_upload(upload.filename, raw)
            scan = scan_uploads(db, path)
        except Exception as exc:
            results.append({"file": upload.filename, "ok": False, "message": str(exc)})
            continue
        if scan["exists"]:
            ex = scan["existing"] or {}
            pending.append({
                "file": upload.filename,
                "path": str(path),
                "invoice_no": scan["invoice_no"],
                "uploaded_at": ex.get("uploaded_at").strftime("%Y-%m-%d %H:%M")
                if ex.get("uploaded_at") else "-",
                "uploaded_by": ex.get("uploaded_by") or "-",
                "source_filename": ex.get("source_filename") or "-",
            })
        else:
            try:
                invoice, created = import_source_file(db, path, user)
                results.append(
                    {"file": upload.filename, "ok": True,
                     "invoice_no": invoice.invoice_no,
                     "message": "新建" if created else "替换已有发票"}
                )
                audit(db, user, "import", f"{upload.filename} -> {invoice.invoice_no}")
            except Exception as exc:
                db.rollback()
                results.append({"file": upload.filename, "ok": False, "message": str(exc)})

    ok = sum(1 for r in results if r["ok"])
    if results:
        audit(db, user, "import.batch", f"上传 {len(results)} 个，成功 {ok} 个")
    return templates.TemplateResponse(
        request,
        "admin_upload.html",
        {"user": user, "results": results, "pending": pending,
         "flash": request.session.pop("flash", None)},
    )


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
        {i.id: i.invoice_no for i in db.query(Invoice).filter(Invoice.id.in_(all_ids))}
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
