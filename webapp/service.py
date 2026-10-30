# -*- coding: utf-8 -*-
"""编辑 / 导出用例编排。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from loguru import logger
from sqlalchemy.orm import Session

from core.config import (
    ENABLE_TRACK_CHANGES,
    HISTORY_DIR,
    OUTPUT_DIR,
    ensure_dirs,
)
from core.exporter import (
    BuildResult,
    Exporter,
    ManualFields,
    _split_label,
    add_trace_sheet,
    enable_track_changes,
    parse_unit_prices,
)
from core.shipment_list import (
    ShipmentRow,
    build_row_from_result,
    eta_for,
    month_of,
    month_path,
    parse_date,
    write_workbook,
)
from core.source_reader import SourceData, read_source_file
from .timefmt import fmt_local
from .converters.field_map import ALLOWED_KEYS, editable_cells, validate_form
from .converters.univer_snapshot import workbook_to_snapshot
from .models import (
    ROLE_ADMIN,
    ROLE_TYPE1,
    ROLE_TYPE2,
    ROLE_TYPE3,
    ExportFileVersion,
    ExportJob,
    Invoice,
    ShipmentListEntry,
    User,
)

_exporter: Optional[Exporter] = None
_source_cache: dict[tuple, SourceData] = {}


def get_exporter() -> Exporter:
    """导出器单例（模板 / 单价表只读一次）。"""
    global _exporter
    if _exporter is None:
        _exporter = Exporter()
    return _exporter


def load_sources(invoices: list[Invoice]) -> list[SourceData]:
    """按发票记录回读源文件（按 mtime+size 缓存解析结果，编辑页提速）。"""
    sources: list[SourceData] = []
    for invoice in invoices:
        path = Path(invoice.source_file)
        if not path.exists():
            raise ValueError(f"Source file not found: {invoice.source_filename}")
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        cached = _source_cache.get(key)
        if cached is None:
            if len(_source_cache) > 200:
                _source_cache.clear()
            cached = read_source_file(path)
            _source_cache[key] = cached
        sources.append(cached)
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
    """构建编辑页预览工作簿。

    人工字段整体留空，但单价的手动覆盖值要带进来（预览显示实际生效单价）。
    """
    if not invoices:
        raise ValueError("No invoice selected")
    sources = load_sources(invoices)
    groups = group_sources(sources)
    if len(groups) > 1:
        names = [g[0].delivery_to or "UNKNOWN" for g in groups]
        raise ValueError(
            "The selected invoices have different Delivery to values and cannot be merged for export: "
            + " / ".join(names)
        )
    stored = _stored_manual(invoices)
    exporter = get_exporter()
    result = exporter.build(
        groups[0],
        ManualFields(unit_prices=parse_unit_prices(stored.get("unit_prices"))),
    )
    if not result.ok:
        raise ValueError("; ".join(result.messages) or "Build failed")
    return result, sources


_snapshot_cache: dict[tuple, dict[str, Any]] = {}


def _snapshot_key(invoices: list[Invoice]) -> tuple:
    """快照缓存键：发票 id + 人工字段内容/保存时间 + 源文件 mtime/size。"""
    parts = []
    for i in sorted(invoices, key=lambda x: x.id):
        src = Path(i.source_file)
        try:
            st = src.stat()
            src_key = (st.st_mtime_ns, st.st_size)
        except OSError:
            src_key = (0, 0)
        parts.append(
            (
                i.id,
                i.manual_values or "",
                str(i.manual_saved_at) if i.manual_saved_at else "",
                src_key,
            )
        )
    return tuple(parts)


def build_snapshot(invoices: list[Invoice]) -> dict[str, Any]:
    """生成编辑页所需的完整数据（带缓存，重复打开显著提速）。"""
    key = _snapshot_key(invoices)
    cached = _snapshot_cache.get(key)
    if cached is not None:
        return cached

    result, sources = build_for_edit(invoices)

    first = sources[0]
    price_rows = _price_rows(result, sources, result.company)
    cells = editable_cells(result.transport, result.container_row)
    # 单价单元格允许在表格里直接编辑（查不到单价的橙色格子仍可改，但不覆盖橙色底纹）
    price_cells = [f"I{p['row']}" for p in price_rows]
    highlight_cells = {
        sheet: list(coords) for sheet, coords in cells.items()
    }
    if price_cells:
        allow = [c for c, p in zip(price_cells, price_rows) if not p["missing"]]
        highlight_cells["INV1"] = sorted(
            set(highlight_cells.get("INV1", []) + allow),
            key=lambda x: int("".join(filter(str.isdigit, x)) or 0),
        )
        cells["INV1"] = sorted(
            set(cells.get("INV1", []) + price_cells),
            key=lambda x: int("".join(filter(str.isdigit, x)) or 0),
        )
    snapshot = workbook_to_snapshot(
        result.workbook,
        workbook_id=f"invoice-{invoices[0].id}",
        workbook_name=result.file_stem,
        highlight=highlight_cells,
    )
    result.workbook.close()

    missing_prices = [p["fabric_type"] for p in price_rows if p["missing"]]
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

    payload = {
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
            "last_saved_by": next(
                (i.manual_saved_by for i in invoices if i.manual_saved_by), None
            ),
            "last_saved_at": next(
                (
                    i.manual_saved_at.strftime("%Y-%m-%d %H:%M:%S")
                    for i in invoices
                    if i.manual_saved_at
                ),
                None,
            ),
            "missing_prices": missing_prices,
            "prices": price_rows,
        },
    }
    if len(_snapshot_cache) > 30:
        _snapshot_cache.clear()
    _snapshot_cache[key] = payload
    return payload


def _price_rows(
    result: BuildResult, sources: list[SourceData], company: str
) -> list[dict[str, Any]]:
    """编辑页单价区数据（与导出表 G/I/J 列完全一致）。

    直接读取构建好的 INV1 取行号与生效单价，同时汇总各面料净重，
    供前端按导出公式（净重×比例×单价）实时计算金额。
    """
    from core.price_table import lookup_price, weight_ratio

    exporter = get_exporter()
    ratio = weight_ratio(sources[0].ship_mode) if sources else 0.94
    agg: dict[str, float] = {}
    for data in sources:
        for po in data.po_data:
            ft = (po.get("fabric_type") or "").strip() or "UNKNOWN"
            agg[ft.upper()] = agg.get(ft.upper(), 0.0) + float(
                po.get("net_weight") or 0
            )
    ws = result.workbook["INV1"]
    last = result.last_data_row or 24
    rows: list[dict[str, Any]] = []
    for r in range(24, last + 1):
        d_val = ws[f"D{r}"].value
        i_val = ws[f"I{r}"].value
        if not d_val or i_val is None:
            continue
        fabric_type = str(d_val).strip()
        key = fabric_type.upper()
        price = float(i_val)
        rows.append(
            {
                "key": key,
                "fabric_type": fabric_type,
                "row": r,
                "weight": round(agg.get(key, 0.0), 2),
                "ratio": ratio,
                "ratio_pct": f"{ratio * 100:.0f}%",
                "price": price,
                "auto_price": lookup_price(exporter.prices, company, fabric_type),
                "customs_weight": int(ws[f"G{r}"].value or 0),
                "amount": round(float(ws[f"J{r}"].value or 0), 2),
                "missing": price == 0.0,
                "overridden": price > 0
                and price != lookup_price(exporter.prices, company, fabric_type),
            }
        )
    return rows


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


def save_manual_values(
    db: Session,
    invoices: list[Invoice],
    values: dict[str, str],
    user: Optional[User] = None,
) -> None:
    """把人工字段写回组内所有发票（合并导出共享同一份值）。"""
    payload = json.dumps(values, ensure_ascii=False)
    now = datetime.utcnow()
    for invoice in invoices:
        invoice.manual_values = payload
        if user is not None:
            invoice.manual_saved_by = user.username
            invoice.manual_saved_at = now
    db.commit()


def _field_applicable(key: str, result: BuildResult) -> bool:
    if key == "port_loading":
        return result.transport == "陆运"
    if key == "container_info":
        return result.container_row is not None
    if key == "flight_no":
        return result.transport == "空运"
    if key == "unit_prices":
        # 单价在「自动计算」区按面料类型单独渲染，不作为普通输入框
        return False
    return True


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _export_seq(db: Session, folder: str, filename: str) -> int:
    """该文件第几次生成（仅用于隐藏追踪表序号，不体现在文件名中）。"""
    n = (
        db.query(ExportFileVersion)
        .filter(
            ExportFileVersion.kind == "export",
            ExportFileVersion.folder == folder,
            ExportFileVersion.filename == filename,
        )
        .count()
    )
    return n + 1


def _archive_history(db: Session, folder: str, filename: str, out_path: Path) -> str:
    """把当前正式文件移入内部留底目录（时间戳命名，不对外展示为文件名）。"""
    if not out_path.exists():
        return ""
    hist_dir = HISTORY_DIR / folder
    hist_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(filename).stem
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    dest = hist_dir / f"{stem}.{stamp}.xlsm"
    # 同一秒内的多次留底不能互相覆盖，重名时追加序号
    seq = 1
    while dest.exists():
        seq += 1
        dest = hist_dir / f"{stem}.{stamp}_{seq}.xlsm"
    out_path.replace(dest)
    return str(dest)


def _apply_shipment_overrides(row: ShipmentRow, raw: dict[str, Any]) -> None:
    """把导出弹窗里手工填写的装柜列表字段覆盖到行上（空值跳过）。"""
    def _s(key: str) -> str:
        value = raw.get(key)
        return "" if value is None else str(value).strip()

    package_text = _s("package")
    if package_text:
        try:
            row.package = float(package_text)
        except (TypeError, ValueError):
            pass
    if _s("etd"):
        row.etd = parse_date(_s("etd"))
    if _s("eta"):
        row.eta = parse_date(_s("eta"))
    elif row.etd is not None:
        # 只填了 ETD 没填 ETA 时，按运输方式自动推算
        row.eta = eta_for(row.mode, row.etd)
    if _s("vessel"):
        row.vessel = _s("vessel")
    if _s("carrier"):
        row.carrier = _s("carrier")
    if _s("container_type"):
        row.container_type = _s("container_type")
    if _s("cbm"):
        try:
            row.cbm = float(_s("cbm"))
        except (TypeError, ValueError):
            pass


def export_invoices(
    db: Session,
    invoices: list[Invoice],
    form_values: dict[str, Any],
    user: Optional[User] = None,
    shipment: Optional[dict[str, Any]] = None,
) -> tuple[Path, dict[str, Any]]:
    """生成最终 .xlsm（版本化落盘、注入追踪表）并记录导出历史。

    Returns:
        (文件路径, 结果信息)
    """
    values, errors = validate_form(form_values)
    if errors:
        raise ValueError("；".join(errors))

    sources = load_sources(invoices)
    groups = group_sources(sources)
    if len(groups) > 1:
        raise ValueError("The selected invoices have different Delivery to values and cannot be merged for export")

    exporter = get_exporter()
    manual = ManualFields.from_dict(values)
    result = exporter.build(groups[0], manual)
    if not result.ok:
        raise ValueError("; ".join(result.messages) or "Build failed")

    # 从清关文件工作簿提取装柜列表行（须在 save_xlsm 关闭工作簿前完成）
    shipment_row = build_row_from_result(
        result.workbook,
        company=result.company,
        transport=result.transport,
        customs_weight=result.customs_weight,
    )
    # 权限分割：第二类账号只编辑报关发票本身，导出附带的装柜补充信息一律忽略
    if not can_edit_shipment_info(user):
        shipment = None
    if shipment:
        _apply_shipment_overrides(shipment_row, shipment)
    # 弹窗手填的 ID 仅存数据库（不进 Excel 行），留空保留原值；
    # ID 仅第三类账号 / 管理员可写（权限分割）
    ref_id = str((shipment or {}).get("ref_id") or "").strip()
    if ref_id and not can_edit_ref_id(user):
        ref_id = ""

    ensure_dirs()
    folder = _safe(values["bill_no"]) or "Unnamed"
    final_name = f"{result.file_stem}.xlsm"
    seq = _export_seq(db, folder, final_name)
    out_dir = OUTPUT_DIR / folder
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / final_name
    backup_path = _archive_history(db, folder, final_name, out_path)

    job = ExportJob(
        invoice_ids=json.dumps([i.id for i in invoices]),
        form_values=json.dumps(values, ensure_ascii=False),
        filename=f"{folder}/{final_name}",
        success=True,
        message="；".join(result.messages),
        customs_weight=result.customs_weight,
        total_amount=result.total_amount,
        created_by=user.username if user else None,
    )
    db.add(job)
    db.flush()

    trace_meta = {
        "JOB_ID": str(job.id),
        "VERSION": str(seq),
        "EXPORTED_BY": user.username if user else "-",
        "EXPORTED_AT": fmt_local(datetime.utcnow()),
        "FILE_STEM": result.file_stem,
        "INVOICE_IDS": ",".join(str(i.id) for i in invoices),
        "INVOICE_NOS": ",".join(i.invoice_no or "" for i in invoices),
    }
    add_trace_sheet(result.workbook, trace_meta)
    exporter.save_xlsm(result.workbook, out_path, _carrier_for(sources))

    track_changes = False
    if ENABLE_TRACK_CHANGES:
        try:
            track_changes = enable_track_changes(out_path)
        except Exception:
            logger.exception("开启修订跟踪异常")
        if track_changes:
            logger.info("已开启修订跟踪: {}", out_path.name)
        else:
            logger.warning("未能开启修订跟踪（Excel 不可用或文件被占用）: {}", out_path.name)

    sha = _sha256(out_path)
    db.add(
        ExportFileVersion(
            job_id=job.id,
            version=seq,
            kind="export",
            folder=folder,
            filename=final_name,
            file_path=str(out_path),
            sha256=sha,
            invoice_ids=json.dumps([i.id for i in invoices]),
            form_values=json.dumps(values, ensure_ascii=False),
            backup_path=backup_path,
            created_by=user.username if user else None,
        )
    )
    # 导出成功即持久化人工字段，下次打开编辑页自动回填
    saved_at = datetime.utcnow()
    for invoice in invoices:
        invoice.manual_values = json.dumps(values, ensure_ascii=False)
        if user is not None:
            invoice.manual_saved_by = user.username
            invoice.manual_saved_at = saved_at
    db.commit()

    list_info: dict[str, Any] = {}
    try:
        list_info = record_shipment_list(
            db,
            job.id,
            invoices,
            shipment_row,
            user=user,
            ref_id=ref_id,
        )
    except Exception as exc:  # pragma: no cover
        logger.exception("生成装柜列表失败")
        result.messages.append(f"Shipment List generation failed: {exc}")

    logger.info(
        "导出 {}（第 {} 次）by {}",
        out_path.name,
        seq,
        user.username if user else "-",
    )
    if backup_path:
        logger.info("上一版已留底: {}", backup_path)
    # 回传装柜补充信息（含 PKL 自动提取的 Package），供编辑页生成后弹窗自动预填
    shipment_payload: dict[str, Any] = {**shipment_row.as_dict(), "ref_id": ref_id}
    return out_path, {
        "filename": out_path.name,
        "path": str(out_path),
        "folder": folder,
        "version": seq,
        "sha256": sha,
        "saved_at": saved_at.strftime("%Y-%m-%d %H:%M:%S"),
        "customs_weight": result.customs_weight,
        "total_amount": result.total_amount,
        "messages": result.messages,
        "track_changes": track_changes,
        "shipment": shipment_payload,
        **list_info,
    }


def _entry_containers(entry: ShipmentListEntry) -> list[str]:
    """柜号 1-4 按位还原（中间空格保留，只去掉尾部空位）。"""
    cells = [
        (entry.container_1 or "").strip(),
        (entry.container_2 or "").strip(),
        (entry.container_3 or "").strip(),
        (entry.container_4 or "").strip(),
    ]
    while cells and not cells[-1]:
        cells.pop()
    return cells


def _entry_to_row(entry: ShipmentListEntry) -> ShipmentRow:
    """数据库记录还原成装柜列表行。"""
    return ShipmentRow(
        date=parse_date(entry.date),
        fty_inv=entry.fty_inv or "",
        shipper=entry.shipper or "",
        est_vco=entry.est_vco or "",
        custom_no=entry.custom_no or "",
        package=entry.package,
        blading=entry.blading or "",
        containers=_entry_containers(entry),
        nw_kg=float(entry.nw_kg or 0),
        gw_kg=float(entry.gw_kg or 0),
        description=entry.description or "",
        mode=entry.mode or "",
        etd=parse_date(entry.etd),
        eta=parse_date(entry.eta),
        pol=entry.pol or "",
        vessel=entry.vessel or "",
        carrier=entry.carrier or "",
        container_type=entry.container_type or "",
        cbm=entry.cbm,
        machine_cabinet=entry.machine_cabinet or "",
        aux_cabinet=entry.aux_cabinet or "",
    )


def rebuild_shipment_month(db: Session, month: str) -> Path:
    """按数据库记录重写某个月的装柜列表 Excel。"""
    entries = (
        db.query(ShipmentListEntry)
        .filter(ShipmentListEntry.month == month)
        .order_by(ShipmentListEntry.date.asc(), ShipmentListEntry.id.asc())
        .all()
    )
    out_path = month_path(month)
    if not entries:
        out_path.unlink(missing_ok=True)
        return out_path
    return write_workbook([_entry_to_row(e) for e in entries], out_path)


def record_shipment_list(
    db: Session,
    job_id: Optional[int],
    invoices: list[Invoice],
    row: ShipmentRow,
    *,
    user: Optional[User] = None,
    ref_id: str = "",
) -> dict[str, Any]:
    """导出成功后写入装柜列表并刷新当月 Excel。

    以「源文件发票号集合（排序）+ 工厂」（source_key）为业务主键，重复导出同一批
    源数据时更新原行，与手填 Invoice No.（custom_no）无关。装柜列表数据由清关文件
    （INV1/PKL1）提取，调用前已构建为 row。ref_id 为弹窗手填的 ID，仅存数据库
    不写入 Excel；留空则保留原值。
    """
    month = month_of(row.date)
    payload = row.as_dict()

    # 稳定的去重键：源文件发票号集合（排序）+ 工厂
    invoice_nos = sorted({i.invoice_no for i in invoices if i.invoice_no})
    source_key = f"{row.est_vco or ''}:{','.join(invoice_nos)}" if invoice_nos else ""

    entry = None
    if source_key:
        entry = (
            db.query(ShipmentListEntry)
            .filter(ShipmentListEntry.source_key == source_key)
            .one_or_none()
        )
    if entry is None:
        entry = ShipmentListEntry(custom_no=row.custom_no)

    previous_month = entry.month or ""
    # 手动字段（Package/ETD/ETA/船名/承运人/柜型/CBM/机器柜/辅料柜）留空保留原值，
    # 与编辑页弹窗「留空保留」语义一致，避免重新导出清空已填写的内容；清空请在
    # 装柜列表页行内编辑（空值 = 清空）。机器柜/辅料柜只在装柜列表页手填，
    # 导出行恒为空，这里保证重导出不覆盖页面填写的值。
    keep_if_blank = {
        "package", "etd", "eta", "vessel", "carrier", "container_type", "cbm",
        "machine_cabinet", "aux_cabinet",
    }
    for key, value in payload.items():
        blank = value is None or (isinstance(value, str) and not value.strip())
        if key in keep_if_blank and blank and getattr(entry, key, None):
            continue
        setattr(entry, key, value)
    entry.source_key = source_key
    entry.month = month
    entry.job_id = job_id
    entry.invoice_ids = json.dumps([i.id for i in invoices])
    if ref_id:
        entry.ref_id = ref_id
    entry.created_by = entry.created_by or (user.username if user else None)
    entry.updated_at = datetime.utcnow()
    db.add(entry)
    db.commit()

    if previous_month and previous_month != month:
        rebuild_shipment_month(db, previous_month)
    path = rebuild_shipment_month(db, month)
    logger.info("装柜列表已更新: {} by {}", path.name, user.username if user else "-")
    return {
        "list_month": month,
        "list_filename": path.name,
        "list_download": f"/api/shipment-list/download?month={month}",
        "list_url": f"/shipments?month={month}",
    }


# ---- Shipment List 新增行（纯手工空行） ----


def add_empty_shipment_row(
    db: Session,
    user: Optional[User] = None,
) -> tuple[ShipmentListEntry, dict[str, Any]]:
    """Shipment List 新增行：直接落一条空记录，全部字段行内手填。

    不生成报关单文件、不关联 Invoice / ExportJob / 源文件；报关单文件名
    （Customs Reference Number，custom_ref）为纯文本手填，无真实文件。
    顺带刷新当月装柜列表 Excel。

    Args:
        db: 数据库会话。
        user: 操作人。

    Returns:
        (新建记录, 结果信息含 entry_id / list_month / list_url)。
    """
    month = month_of(None)
    entry = ShipmentListEntry(
        month=month,
        is_manual=True,
        created_by=user.username if user else None,
    )
    db.add(entry)
    db.commit()
    path = rebuild_shipment_month(db, month)
    logger.info("装柜列表新增空行 by {}", user.username if user else "-")
    return entry, {
        "entry_id": entry.id,
        "list_month": month,
        "list_filename": path.name,
        "list_url": f"/shipments?month={month}",
    }


# ---- Shipment List 行内编辑（三路同步：平台 / .xlsm / 发票明细） ----


def can_edit_shipment_info(user: Optional[User]) -> bool:
    """装柜列表补充信息（导出弹窗字段）仅第一 / 第三类与管理员可写。

    权限分割：第二类账号的编辑权限只覆盖报关发票本身（含重新生成），
    不得顺带改写装柜列表的手动字段。

    Args:
        user: 当前用户；None（系统调用）视为允许。

    Returns:
        是否可写装柜补充信息。
    """
    return user is None or user.role != ROLE_TYPE2


def can_edit_ref_id(user: Optional[User]) -> bool:
    """ID（ref_id）字段第一类 / 第三类账号与管理员可改（权限分割）。

    Args:
        user: 当前用户；None（系统调用）视为允许。

    Returns:
        是否可写 ID 字段。
    """
    return user is None or user.role in (ROLE_TYPE1, ROLE_TYPE3, ROLE_ADMIN)


#: 行内编辑可写字段（与 ShipmentListEntry 列名一致）
ENTRY_EDIT_FIELDS: tuple[str, ...] = (
    "date", "fty_inv", "shipper", "est_vco", "custom_no", "package", "blading",
    "container_1", "container_2", "container_3", "container_4",
    "nw_kg", "gw_kg", "description", "mode", "etd", "eta", "pol",
    "vessel", "carrier", "container_type", "cbm",
    "machine_cabinet", "aux_cabinet", "ref_id", "custom_ref",
)

#: 手动填入字段（装柜补充信息弹窗 + Shipment List 页手填），第三类账号仅可改这些
MANUAL_EDIT_FIELDS: tuple[str, ...] = (
    "package", "etd", "eta", "vessel", "carrier", "container_type", "cbm",
    "machine_cabinet", "aux_cabinet", "ref_id",
)


def editable_entry_fields(user: Optional[User], is_manual: bool = False) -> tuple[str, ...]:
    """按角色返回 Shipment List 行内可编辑字段（权限分割）。

    Shipment List 行内编辑对第一类 / 第三类 / 管理员开放手动填入字段（ID、
    Package、ETD、ETA、VESSEL、Carrier、Container Type、CBM、Machine
    Cabinet、Aux Cabinet）；报关发票的整张编辑通过「Edit」按钮跳转到发票明细
    编辑页（仅第一类 / 管理员）。手工新增的空行（is_manual）无源文件可跳转，
    因此整行全部字段（含 Customs Reference Number 文件名）都在行内手填。
    第二类账号看不见装柜列表，无可编辑字段。

    Args:
        user: 当前用户；None（系统调用）视为可编辑。
        is_manual: 是否为手工新增空行；True 时返回全部字段。

    Returns:
        可编辑字段名元组（:data:`ENTRY_EDIT_FIELDS` 的子集）。
    """
    if user is None or user.role in (ROLE_TYPE1, ROLE_TYPE3, ROLE_ADMIN):
        if is_manual:
            return ENTRY_EDIT_FIELDS
        return MANUAL_EDIT_FIELDS
    return ()

#: 字段显示名（审计日志 / 修改记录共用，与 Shipment List 表头一致）
ENTRY_FIELD_LABELS: dict[str, str] = {
    "date": "Date", "fty_inv": "Fty Inv", "shipper": "Shipper", "est_vco": "EST/VCO",
    "custom_no": "Custom #", "package": "Package", "blading": "Blading",
    "container_1": "Container 1", "container_2": "Container 2",
    "container_3": "Container 3", "container_4": "Container 4",
    "nw_kg": "NW KG", "gw_kg": "GW KG", "description": "DESCRIPTION", "mode": "Mode",
    "etd": "ETD", "eta": "ETA", "pol": "POL", "vessel": "VESSEL", "carrier": "Carrier",
    "container_type": "Container Type", "cbm": "CBM",
    "machine_cabinet": "Machine Cabinet", "aux_cabinet": "Aux Cabinet", "ref_id": "ID",
    "custom_ref": "Customs Reference Number",
}

_CONTAINER_FIELDS = ("container_1", "container_2", "container_3", "container_4")

#: 能对应到 .xlsm 单元格的字段（改动时改写报关发票文件并留修改记录）
_XLSM_FIELDS = frozenset(
    {"date", "mode", "pol", "package", "gw_kg", "nw_kg", *_CONTAINER_FIELDS}
)

_SHIP_MODE_TEXT = {"AIR": "by Air", "SEA": "by Sea", "TRUCK": "by Truck"}


def _ship_mode_text(mode: str) -> str:
    """Mode（AIR/SEA/TRUCK）→ .xlsm B17 的船务表述。"""
    return _SHIP_MODE_TEXT.get((mode or "").upper(), mode or "")


def _display(value: Any) -> str:
    """字段值的展示文本（None 归一成空串，便于比较与日志）。"""
    return "" if value is None else str(value)


def _write_place_cell(cell: Any, value: str) -> None:
    """保留模板标签前缀改写起运地/装货港单元格（与 exporter 写法一致）。"""
    prefix, _ = _split_label(cell.value)
    text = (value or "").strip()
    if not text:
        cell.value = None
    elif prefix.strip() and text.lower().startswith(prefix.strip().lower()):
        cell.value = text
    else:
        cell.value = f"{prefix}{text}"


def _redistribute_nw(ws: Any, new_total: float) -> None:
    """按现值比例重写 INV1 G 列报关重量（尾差补最后一个非零行）。"""
    total_row = None
    for r in range(24, 60):
        c_val = ws[f"C{r}"].value
        if c_val and "Total" in str(c_val):
            total_row = r
            break
    rows: list[tuple[int, float]] = []
    for r in range(24, total_row or 60):
        val = ws[f"G{r}"].value
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val:
            rows.append((r, float(val)))
    if not rows:
        return
    old_sum = sum(v for _, v in rows)
    if old_sum <= 0:
        return
    target = round(new_total, 2)
    scaled: list[tuple[int, float]] = []
    for r, v in rows[:-1]:
        scaled.append((r, round(v * target / old_sum, 2)))
    scaled.append((rows[-1][0], round(target - sum(v for _, v in scaled), 2)))
    for r, v in scaled:
        ws[f"G{r}"] = v


def _snapshot_pairs(entry: ShipmentListEntry, changes: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    """行内编辑字段 → 修改记录快照键（沿用导出 form_values 的键名）。"""
    changed = {c["key"] for c in changes}
    pairs: list[tuple[str, Any]] = []
    if "date" in changed:
        pairs.append(("date", entry.date or ""))
    if "mode" in changed:
        pairs.append(("ship_mode", _ship_mode_text(entry.mode)))
    if "pol" in changed:
        key = "port_loading" if (entry.mode or "").upper() == "TRUCK" else "place_receipt"
        pairs.append((key, entry.pol or ""))
    if changed & set(_CONTAINER_FIELDS):
        pairs.append(("container_info", "\n".join(_entry_containers(entry))))
    simple = {
        "package", "nw_kg", "gw_kg", "etd", "eta", "vessel", "carrier",
        "container_type", "cbm", "fty_inv", "shipper", "est_vco",
        "custom_no", "blading", "description", "ref_id",
    }
    for key in simple & changed:
        value = getattr(entry, key)
        pairs.append((key, "" if value is None else value))
    return pairs


def _merged_snapshot(
    db: Session,
    folder: str,
    filename: str,
    entry: ShipmentListEntry,
    changes: list[dict[str, Any]],
) -> dict[str, Any]:
    """在上一份 form_values 快照上合并本次变更，供修改记录展示。

    ``_changes`` 直接记录精确的字段 old→new（时间线优先用它，避免跨记录
    快照 diff 的噪音）；其余键沿用导出快照键名，完整字段一栏展示现值。
    """
    prev = (
        db.query(ExportFileVersion)
        .filter(
            ExportFileVersion.folder == folder,
            ExportFileVersion.filename == filename,
            ExportFileVersion.form_values.notin_(("", "{}")),
        )
        .order_by(ExportFileVersion.id.desc())
        .first()
    )
    snapshot: dict[str, Any] = {}
    if prev is not None:
        try:
            loaded = json.loads(prev.form_values or "{}")
            if isinstance(loaded, dict):
                snapshot = loaded
        except (TypeError, ValueError):
            snapshot = {}
    for key, value in _snapshot_pairs(entry, changes):
        snapshot[key] = value
    snapshot["_changes"] = changes
    return snapshot


def _sync_container_manual_values(
    db: Session, entry: ShipmentListEntry, user: Optional[User] = None
) -> None:
    """柜号改动写回发票 manual_values["container_info"]（明细页 Container No. 即时一致）。"""
    try:
        ids = [int(x) for x in json.loads(entry.invoice_ids or "[]")]
    except (TypeError, ValueError):
        return
    if not ids:
        return
    joined = "\n".join(_entry_containers(entry))
    now = datetime.utcnow()
    for invoice in db.query(Invoice).filter(Invoice.id.in_(ids)).all():
        try:
            data = json.loads(invoice.manual_values or "{}")
        except (TypeError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data["container_info"] = joined
        invoice.manual_values = json.dumps(data, ensure_ascii=False)
        if user is not None:
            invoice.manual_saved_by = user.username
            invoice.manual_saved_at = now
    db.commit()


def sync_entry_to_xlsm(
    db: Session,
    entry: ShipmentListEntry,
    changes: list[dict[str, Any]],
    user: Optional[User] = None,
) -> dict[str, Any]:
    """把 Shipment List 行内编辑改写回报关发票 .xlsm 并留修改记录。

    仅处理能对应到单元格的字段（Date/Mode/POL/柜号/Package/GW/NW）；改写前把
    旧文件移入 file_history 留底，改写后按新文件内容补一条 kind="save" 记录
    （带 sha256、留底下载与字段变更），挂到该文件的修改记录时间线上。

    Args:
        db: 数据库会话。
        entry: 已落库的装柜列表行。
        changes: 字段级变更（{key, label, old, new}）。
        user: 操作人。

    Returns:
        {"synced": 是否改写了文件, "message": 提示信息}
    """
    hit = [c for c in changes if c["key"] in _XLSM_FIELDS]
    if not hit:
        return {"synced": False, "message": ""}

    # 手工新增空行无报关发票文件，仅落库，不做文件改写
    if entry.is_manual:
        return {"synced": False, "message": ""}

    job = db.get(ExportJob, entry.job_id) if entry.job_id else None
    folder = name = ""
    if job and job.filename:
        folder, _, name = job.filename.partition("/")
    out_path = OUTPUT_DIR / folder / name if name else None
    if out_path is None or not out_path.is_file():
        return {"synced": False, "message": "Customs invoice file not found, platform data updated only"}

    import openpyxl
    from openpyxl.cell.cell import MergedCell

    changed = {c["key"] for c in hit}
    wb = openpyxl.load_workbook(str(out_path), keep_vba=True)
    try:
        ws = wb["INV1"]
        if "date" in changed:
            ws["J9"] = entry.date
        if "mode" in changed:
            ws["B17"] = _ship_mode_text(entry.mode)
        if "pol" in changed:
            if (entry.mode or "").upper() == "TRUCK":
                # 陆运 POL 来自装货港 A19，写回时保留 "Port of loading:" 标签
                cell = ws["A19"]
                value = (entry.pol or "").strip()
                if not value:
                    cell.value = None
                elif value.lower().startswith("port of loading:"):
                    cell.value = value
                else:
                    cell.value = f"Port of loading: {value}"
            else:
                _write_place_cell(ws["A18"], entry.pol or "")
        if changed & set(_CONTAINER_FIELDS):
            container_row = None
            for r in range(24, 60):
                a_val = ws[f"A{r}"].value
                if a_val and "CONTAINER" in str(a_val).upper():
                    container_row = r
                    break
            if container_row is not None:
                for r in range(container_row + 1, container_row + 10):
                    for col in ("A", "B"):
                        cell = ws[f"{col}{r}"]
                        if not isinstance(cell, MergedCell):
                            cell.value = None
                for i, line in enumerate(_entry_containers(entry)[:4]):
                    ws[f"A{container_row + 1 + i}"] = line
        if "PKL1" in wb.sheetnames:
            ws_pkl = wb["PKL1"]
            if "package" in changed:
                ws_pkl["A24"] = entry.package
            if "gw_kg" in changed:
                ws_pkl["K24"] = float(entry.gw_kg or 0)
        if "nw_kg" in changed:
            _redistribute_nw(ws, float(entry.nw_kg or 0))

        backup = _archive_history(db, folder, name, out_path)
        wb.save(str(out_path))
    finally:
        wb.close()

    sha = _sha256(out_path)
    snapshot = _merged_snapshot(db, folder, name, entry, changes)
    db.add(
        ExportFileVersion(
            job_id=entry.job_id,
            kind="save",
            version=0,
            folder=folder,
            filename=name,
            file_path=str(out_path),
            sha256=sha,
            invoice_ids=entry.invoice_ids or "[]",
            form_values=json.dumps(snapshot, ensure_ascii=False),
            backup_path=backup,
            created_by=user.username if user else None,
        )
    )
    db.commit()
    logger.info(
        "报关发票已同步改写: {}/{} by {}", folder, name, user.username if user else "-"
    )
    return {"synced": True, "message": ""}


def update_shipment_entry(
    db: Session,
    entry: ShipmentListEntry,
    payload: dict[str, Any],
    user: Optional[User] = None,
) -> dict[str, Any]:
    """Shipment List 行内编辑保存：全量落库并同步 .xlsm / 发票明细。

    空值 = 清空（所见即所得）；ETA 留空且 ETD 有值时按运输方式自动推算。
    月度 Excel 每次保存后按库内最新数据重建（date 变更导致月份变化时旧、
    新两个月都重建）；柜号变动同步写回
    发票 manual_values；可映射字段变动改写报关发票文件并留修改记录（见
    :func:`sync_entry_to_xlsm`）。source_key 身份键保持不变。

    Args:
        db: 数据库会话。
        entry: 目标装柜列表行（已落库）。
        payload: 全量字段值（缺键视为空）。
        user: 操作人。

    Returns:
        {"ok", "message", "xlsm_synced", "changes", "updated_at"}

    Raises:
        ValueError: 数字字段无法解析。
    """
    previous_month = entry.month or ""
    old_values: dict[str, Any] = {f: getattr(entry, f) for f in ENTRY_EDIT_FIELDS}

    # 权限分割：越权字段一律跳过、保留原值（手工新增空行整行可编辑）
    editable = set(editable_entry_fields(user, entry.is_manual))

    for field in ENTRY_EDIT_FIELDS:
        if field not in editable:
            continue
        raw = payload.get(field)
        text = "" if raw is None else str(raw).strip()
        if field in ("date", "etd", "eta"):
            parsed = parse_date(text)
            setattr(entry, field, parsed.isoformat() if parsed else "")
        elif field in ("package", "cbm"):
            if text:
                try:
                    setattr(entry, field, float(text))
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"{ENTRY_FIELD_LABELS.get(field, field)} must be a number"
                    ) from exc
            else:
                setattr(entry, field, None)
        elif field in ("nw_kg", "gw_kg"):
            try:
                setattr(entry, field, float(text) if text else 0.0)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{ENTRY_FIELD_LABELS.get(field, field)} must be a number"
                ) from exc
        else:
            setattr(entry, field, text)

    # 只填了 ETD 没填 ETA 时，按运输方式自动推算（与导出弹窗一致）
    if not entry.eta and entry.etd:
        derived = eta_for(entry.mode, parse_date(entry.etd))
        entry.eta = derived.isoformat() if derived else ""

    entry.month = month_of(parse_date(entry.date))
    entry.updated_at = datetime.utcnow()
    db.commit()

    changes: list[dict[str, Any]] = []
    for field in ENTRY_EDIT_FIELDS:
        old_text = _display(old_values[field])
        new_text = _display(getattr(entry, field))
        if old_text != new_text:
            changes.append(
                {
                    "key": field,
                    "label": ENTRY_FIELD_LABELS.get(field, field),
                    "old": old_text,
                    "new": new_text,
                }
            )

    # 月度 Excel 每次保存后按库内最新数据重建；date 改到别的月份时新旧两份都重建
    if entry.month:
        rebuild_shipment_month(db, entry.month)
    if previous_month and previous_month != entry.month:
        rebuild_shipment_month(db, previous_month)

    if any(c["key"] in _CONTAINER_FIELDS for c in changes):
        _sync_container_manual_values(db, entry, user)

    xlsm = sync_entry_to_xlsm(db, entry, changes, user)
    if xlsm["synced"]:
        message = "Saved, customs invoice file synced"
    elif xlsm["message"]:
        message = f"Saved ({xlsm['message']})"
    else:
        message = "Saved"

    logger.info(
        "装柜列表行内编辑 #{} by {}: {}",
        entry.id,
        user.username if user else "-",
        "；".join(f"{c['label']} {c['old']}→{c['new']}" for c in changes) or "无变更",
    )
    return {
        "ok": True,
        "message": message,
        "xlsm_synced": xlsm["synced"],
        "changes": changes,
        "updated_at": fmt_local(entry.updated_at),
    }


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


def record_manual_save(
    db: Session,
    invoices: list[Invoice],
    values: dict[str, str],
    user: Optional[User] = None,
) -> None:
    """记录一次编辑保存（修改留痕，不生成文件）。"""
    db.add(
        ExportFileVersion(
            kind="save",
            folder="",
            filename="",
            file_path="",
            version=0,
            invoice_ids=json.dumps([i.id for i in invoices]),
            form_values=json.dumps(values, ensure_ascii=False),
            created_by=user.username if user else None,
        )
    )
    db.commit()


def first_generation_ids(db: Session, folder: str, name: str) -> str:
    """报关发票文件第一次生成时的发票 id 串（如 "1,2"），无生成记录返回 ""。

    装柜列表「报关发票」编辑入口用它还原第一次生成该文件时的发票组合，
    保证进入的编辑页与首次生成一致（后续生成可能换了发票组合）。

    Args:
        db: 数据库会话。
        folder: 输出子目录（bill_no）。
        name: 文件名。

    Returns:
        逗号连接的发票 id；无匹配记录返回空串。
    """
    if not name:
        return ""
    row = (
        db.query(ExportFileVersion)
        .filter(
            ExportFileVersion.kind == "export",
            ExportFileVersion.folder == folder,
            ExportFileVersion.filename == name,
        )
        .order_by(ExportFileVersion.id.asc())
        .first()
    )
    if row is None:
        return ""
    try:
        ids = [int(x) for x in json.loads(row.invoice_ids or "[]")]
    except (TypeError, ValueError):
        return ""
    return ",".join(str(i) for i in ids)


def export_map_for(
    db: Session, invoice_ids: Iterable[int]
) -> dict[int, ExportFileVersion]:
    """返回每张发票最近一次导出记录，供明细页显示生成状态。"""
    targets = {int(i) for i in invoice_ids}
    if not targets:
        return {}
    rows = (
        db.query(ExportFileVersion)
        .filter(ExportFileVersion.kind == "export")
        .order_by(ExportFileVersion.id.desc())
        .all()
    )
    found: dict[int, ExportFileVersion] = {}
    for row in rows:
        try:
            numbers = {int(x) for x in json.loads(row.invoice_ids or "[]")}
        except (TypeError, ValueError):
            continue
        for number in numbers & targets:
            found.setdefault(number, row)
        if len(found) == len(targets):
            break
    return found


def export_bill_no(invoice: Invoice) -> str:
    """导出所用的提单号：操作员填写的 bill_no。

    未填写时返回空字符串（明细页显示为 -，与发票号区分开）；
    导出生效时仍由编辑页默认回填发票号。
    """
    try:
        data = json.loads(invoice.manual_values or "{}")
    except (TypeError, ValueError):
        data = {}
    return str(data.get("bill_no") or "").strip()


def export_container(invoice: Invoice) -> str:
    """明细展示用柜号：优先操作员填写的 container_info，其次源文件柜号。"""
    try:
        data = json.loads(invoice.manual_values or "{}")
    except (TypeError, ValueError):
        data = {}
    lines = [
        line.strip()
        for line in str(data.get("container_info") or "").splitlines()
        if line.strip()
    ]
    return " / ".join(lines) or (invoice.container_number or "").strip()


def company_for_price(invoice: Invoice) -> str:
    """单价表公司口径（与导出逻辑一致）。"""
    if invoice.company in ("Eastex", "Vanco"):
        return invoice.company
    d = (invoice.delivery_to or "").upper()
    if "EASTEX" in d:
        return "Eastex"
    if "VANCO" in d:
        return "Vanco"
    return "Eastex"


def shipment_ref_map(db: Session) -> dict[int, str]:
    """发票 id → 装柜列表 ref_id（第二类账号明细页反查，最新一条优先）。"""
    mapping: dict[int, str] = {}
    entries = (
        db.query(ShipmentListEntry)
        .order_by(ShipmentListEntry.id.desc())
        .all()
    )
    for entry in entries:
        if not entry.ref_id:
            continue
        try:
            ids = [int(i) for i in json.loads(entry.invoice_ids or "[]")]
        except (TypeError, ValueError):
            continue
        for i in ids:
            mapping.setdefault(i, entry.ref_id)
    return mapping


def attach_detail_extras(
    rows: list, ref_map: Optional[dict[int, str]] = None
) -> None:
    """给明细行附加导出口径数据（供明细页展示）。

    附加属性：
        export_bill_no  提单号
        container_no    集装箱柜号
        ref_id          所属装柜手动填写的 ID
        cw              报关净重 = 净重 × 运输比例
        up              单价（价格表查询；None=无面料类型，0.0=未查到）
        ca              报关金额 = cw × up
        in_export       是否落在导出使用的 17-44 行区间
    """
    from core.price_table import lookup_price, weight_ratio
    from core.source_reader import EXPORT_FIRST_ROW, EXPORT_LAST_ROW

    ref_map = ref_map or {}
    exporter = get_exporter()
    for item in rows:
        item.export_bill_no = "-"
        item.container_no = ""
        item.ref_id = ""
        item.cw = None
        item.up = None
        item.ca = None
        item.in_export = True
        invoice = item.invoice
        if invoice is None:
            continue
        item.ref_id = ref_map.get(invoice.id, "")
        ratio = weight_ratio(invoice.ship_mode)
        item.export_bill_no = export_bill_no(invoice)
        item.container_no = export_container(invoice)
        item.cw = (
            round(float(item.net_weight) * ratio, 3)
            if item.net_weight is not None
            else None
        )
        if item.fabric_type:
            item.up = lookup_price(
                exporter.prices, company_for_price(invoice), item.fabric_type
            )
        else:
            item.up = None
        item.ca = round(item.cw * item.up, 2) if item.cw is not None and item.up else None
        item.in_export = EXPORT_FIRST_ROW <= (item.row or 0) <= EXPORT_LAST_ROW
