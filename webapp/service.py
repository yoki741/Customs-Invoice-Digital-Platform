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

from core.config import ENABLE_TRACK_CHANGES, HISTORY_DIR, OUTPUT_DIR, ensure_dirs
from core.exporter import (
    BuildResult,
    Exporter,
    ManualFields,
    add_trace_sheet,
    enable_track_changes,
    parse_unit_prices,
)
from core.shipment_list import (
    ShipmentRow,
    build_row_from_result,
    month_of,
    month_path,
    parse_date,
    write_workbook,
)
from core.source_reader import SourceData, read_source_file
from .converters.field_map import editable_cells, validate_form
from .converters.univer_snapshot import workbook_to_snapshot
from .models import (
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
            raise ValueError(f"源文件不存在: {invoice.source_filename}")
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
        raise ValueError("没有选中的发票")
    sources = load_sources(invoices)
    groups = group_sources(sources)
    if len(groups) > 1:
        names = [g[0].delivery_to or "UNKNOWN" for g in groups]
        raise ValueError(
            "选中的发票 Delivery to 不一致，无法合并导出："
            + " / ".join(names)
        )
    stored = _stored_manual(invoices)
    exporter = get_exporter()
    result = exporter.build(
        groups[0], ManualFields(unit_prices=parse_unit_prices(stored.get("unit_prices")))
    )
    if not result.ok:
        raise ValueError("；".join(result.messages) or "构建失败")
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
    dest = hist_dir / f"{stem}.{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.xlsm"
    out_path.replace(dest)
    return str(dest)


def export_invoices(
    db: Session,
    invoices: list[Invoice],
    form_values: dict[str, Any],
    user: Optional[User] = None,
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
        raise ValueError("选中的发票 Delivery to 不一致，无法合并导出")

    exporter = get_exporter()
    manual = ManualFields.from_dict(values)
    result = exporter.build(groups[0], manual)
    if not result.ok:
        raise ValueError("；".join(result.messages) or "构建失败")

    # 从清关文件工作簿提取装柜列表行（须在 save_xlsm 关闭工作簿前完成）
    shipment_row = build_row_from_result(
        result.workbook,
        company=result.company,
        transport=result.transport,
        customs_weight=result.customs_weight,
    )

    ensure_dirs()
    folder = _safe(values["bill_no"]) or "未命名"
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
        "EXPORTED_AT": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
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
        )
    except Exception as exc:  # pragma: no cover
        logger.exception("生成装柜列表失败")
        result.messages.append(f"装柜列表生成失败: {exc}")

    logger.info(
        "导出 {}（第 {} 次）by {}",
        out_path.name,
        seq,
        user.username if user else "-",
    )
    if backup_path:
        logger.info("上一版已留底: {}", backup_path)
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
        **list_info,
    }


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
        containers=[
            value
            for value in (
                entry.container_1,
                entry.container_2,
                entry.container_3,
                entry.container_4,
            )
            if value
        ],
        nw_kg=float(entry.nw_kg or 0),
        gw_kg=float(entry.gw_kg or 0),
        description=entry.description or "",
        mode=entry.mode or "",
        etd=parse_date(entry.etd),
        eta=parse_date(entry.eta),
        pol=entry.pol or "",
        vessel=entry.vessel or "",
        carrier=entry.carrier or "",
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
) -> dict[str, Any]:
    """导出成功后写入装柜列表并刷新当月 Excel。

    以「清关文件发票号」（custom_no）为业务主键，重复导出同一票时更新原行。
    装柜列表数据由清关文件（INV1/PKL1）提取，调用前已构建为 row。
    """
    month = month_of(row.date)
    payload = row.as_dict()

    entry = None
    if row.custom_no:
        entry = (
            db.query(ShipmentListEntry)
            .filter(ShipmentListEntry.custom_no == row.custom_no)
            .one_or_none()
        )
    if entry is None:
        entry = ShipmentListEntry(custom_no=row.custom_no)

    previous_month = entry.month or ""
    for key, value in payload.items():
        setattr(entry, key, value)
    entry.month = month
    entry.job_id = job_id
    entry.invoice_ids = json.dumps([i.id for i in invoices])
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


def attach_detail_extras(rows: list) -> None:
    """给明细行附加导出口径数据（供明细页展示）。

    附加属性：
        export_bill_no  提单号
        container_no    集装箱柜号
        cw              报关净重 = 净重 × 运输比例
        up              单价（价格表查询；None=无面料类型，0.0=未查到）
        ca              报关金额 = cw × up
        in_export       是否落在导出使用的 17-44 行区间
    """
    from core.price_table import lookup_price, weight_ratio
    from core.source_reader import EXPORT_FIRST_ROW, EXPORT_LAST_ROW

    exporter = get_exporter()
    for item in rows:
        item.export_bill_no = "-"
        item.container_no = ""
        item.cw = None
        item.up = None
        item.ca = None
        item.in_export = True
        invoice = item.invoice
        if invoice is None:
            continue
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
