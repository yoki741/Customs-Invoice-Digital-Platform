# -*- coding: utf-8 -*-
"""SQLAlchemy 模型与数据库会话。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from core.config import DB_PATH, ensure_dirs

# 角色常量
ROLE_ADMIN = "admin"
ROLE_TYPE1 = "type1"  # 第一类账号：列表 + 编辑 + 导出
ROLE_TYPE2 = "type2"  # 第二类账号：仅查看发票明细
ROLE_TYPE3 = "type3"  # 第三类账号：Shipment List + 报关发票
ROLE_LABELS = {
    ROLE_ADMIN: "Admin",
    ROLE_TYPE1: "Type 1 (Business / Customs)",
    ROLE_TYPE2: "Type 2 (Details Viewer)",
    ROLE_TYPE3: "Type 3 (Shipment List / Customs Invoices)",
}


# 核对状态（第二类账号核对，第一类账号修复）
CHECK_PENDING = "pending"
CHECK_PASSED = "passed"
CHECK_FAILED = "failed"
CHECK_LABELS = {
    CHECK_PENDING: "Pending Check",
    CHECK_PASSED: "Check Passed",
    CHECK_FAILED: "Issues Found",
}
CHECK_STYLES = {
    CHECK_PENDING: "secondary",
    CHECK_PASSED: "success",
    CHECK_FAILED: "danger",
}


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default=ROLE_TYPE2, index=True)
    display_name: Mapped[str] = mapped_column(String(64), default="")
    dept: Mapped[str] = mapped_column(String(64), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_no: Mapped[Optional[str]] = mapped_column(String(64), index=True, unique=True)
    invoice_date: Mapped[Optional[str]] = mapped_column(String(32))
    bill_to: Mapped[Optional[str]] = mapped_column(String(255))
    delivery_to: Mapped[str] = mapped_column(String(255), default="", index=True)
    supplier_code: Mapped[Optional[str]] = mapped_column(String(64))
    company: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    transport: Mapped[Optional[str]] = mapped_column(String(16), index=True)
    ship_mode: Mapped[Optional[str]] = mapped_column(String(64))
    ship_term: Mapped[Optional[str]] = mapped_column(String(64))
    vessel_flight: Mapped[str] = mapped_column(String(128), default="")
    etd: Mapped[Optional[str]] = mapped_column(String(32))
    eta: Mapped[Optional[str]] = mapped_column(String(32))
    container_number: Mapped[Optional[str]] = mapped_column(String(64))
    seal_number: Mapped[Optional[str]] = mapped_column(String(64))
    destination: Mapped[Optional[str]] = mapped_column(String(128))
    currency: Mapped[Optional[str]] = mapped_column(String(16))
    customs_agent: Mapped[Optional[str]] = mapped_column(String(64))
    origin_place: Mapped[Optional[str]] = mapped_column(String(128))
    origin_country: Mapped[Optional[str]] = mapped_column(String(64))

    rolls: Mapped[float] = mapped_column(Float, default=0.0)
    net_weight: Mapped[float] = mapped_column(Float, default=0.0)
    gross_weight: Mapped[float] = mapped_column(Float, default=0.0)
    total_length: Mapped[float] = mapped_column(Float, default=0.0)
    total_lbs: Mapped[float] = mapped_column(Float, default=0.0)
    total_amount: Mapped[float] = mapped_column(Float, default=0.0)
    item_count: Mapped[int] = mapped_column(Integer, default=0)

    source_filename: Mapped[str] = mapped_column(String(255))
    source_file: Mapped[str] = mapped_column(String(512))
    uploaded_by: Mapped[Optional[str]] = mapped_column(String(64))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    check_status: Mapped[str] = mapped_column(
        String(16), default=CHECK_PENDING, index=True
    )
    check_note: Mapped[str] = mapped_column(Text, default="")
    checked_by: Mapped[Optional[str]] = mapped_column(String(64))
    checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    # 人工填写字段的持久化 JSON（date/bill_no/ship_mode/... 按 field_map 的 key）
    manual_values: Mapped[str] = mapped_column(Text, default="{}")
    # 最近一次保存人工字段的人 / 时间（并发编辑提醒）
    manual_saved_by: Mapped[Optional[str]] = mapped_column(String(64))
    manual_saved_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    items: Mapped[list["InvoiceItem"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", lazy="selectin"
    )


class InvoiceItem(Base):
    __tablename__ = "invoice_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE"), index=True
    )
    row: Mapped[int] = mapped_column(Integer)
    line_no: Mapped[Optional[str]] = mapped_column(String(32))
    po_no: Mapped[Optional[str]] = mapped_column(String(64))
    description: Mapped[Optional[str]] = mapped_column(Text)
    colour: Mapped[Optional[str]] = mapped_column(String(255))
    size: Mapped[Optional[str]] = mapped_column(String(128))
    fabric_type: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    carton: Mapped[Optional[float]] = mapped_column(Float)
    net_weight: Mapped[Optional[float]] = mapped_column(Float)
    gross_weight: Mapped[Optional[float]] = mapped_column(Float)
    length: Mapped[Optional[float]] = mapped_column(Float)
    quantity: Mapped[Optional[float]] = mapped_column(Float)
    unit_price: Mapped[Optional[float]] = mapped_column(Float)
    amount: Mapped[Optional[float]] = mapped_column(Float)

    invoice: Mapped["Invoice"] = relationship(back_populates="items")


class ExportJob(Base):
    __tablename__ = "export_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_ids: Mapped[str] = mapped_column(Text, default="[]")
    form_values: Mapped[str] = mapped_column(Text, default="{}")
    filename: Mapped[str] = mapped_column(String(255), default="")
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    message: Mapped[str] = mapped_column(Text, default="")
    customs_weight: Mapped[int] = mapped_column(Integer, default=0)
    total_amount: Mapped[float] = mapped_column(Float, default=0.0)
    created_by: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ExportFileVersion(Base):
    """导出文件记录：每次导出/编辑保存各一条（修改留痕）。"""

    __tablename__ = "export_file_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    kind: Mapped[str] = mapped_column(String(16), default="export")  # export / save
    folder: Mapped[str] = mapped_column(String(255), default="")
    filename: Mapped[str] = mapped_column(String(255), default="")
    file_path: Mapped[str] = mapped_column(String(512), default="")
    sha256: Mapped[str] = mapped_column(String(64), default="")
    invoice_ids: Mapped[str] = mapped_column(Text, default="[]")
    form_values: Mapped[str] = mapped_column(Text, default="{}")
    backup_path: Mapped[str] = mapped_column(String(512), default="")
    source_version_id: Mapped[Optional[int]] = mapped_column(Integer)
    source_check: Mapped[str] = mapped_column(String(32), default="")  # ok / missing
    created_by: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ShipmentListEntry(Base):
    """装柜列表（Shipment List）一行，导出时落库并按月汇总成 Excel。"""

    __tablename__ = "shipment_list_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    month: Mapped[str] = mapped_column(String(7), index=True, default="")
    date: Mapped[str] = mapped_column(String(10), default="")
    fty_inv: Mapped[str] = mapped_column(Text, default="")
    shipper: Mapped[str] = mapped_column(String(128), default="")
    est_vco: Mapped[str] = mapped_column(String(32), default="")
    custom_no: Mapped[str] = mapped_column(String(128), index=True, default="")
    # 稳定去重键：源文件发票号集合（排序）+ 工厂，与手填 Invoice No. 无关
    source_key: Mapped[str] = mapped_column(String(512), index=True, default="")
    package: Mapped[Optional[float]] = mapped_column(Float)
    blading: Mapped[str] = mapped_column(String(128), default="")
    container_1: Mapped[str] = mapped_column(String(64), default="")
    container_2: Mapped[str] = mapped_column(String(64), default="")
    container_3: Mapped[str] = mapped_column(String(64), default="")
    container_4: Mapped[str] = mapped_column(String(64), default="")
    nw_kg: Mapped[float] = mapped_column(Float, default=0.0)
    gw_kg: Mapped[float] = mapped_column(Float, default=0.0)
    description: Mapped[str] = mapped_column(Text, default="")
    mode: Mapped[str] = mapped_column(String(16), index=True, default="")
    etd: Mapped[str] = mapped_column(String(10), default="")
    eta: Mapped[str] = mapped_column(String(10), default="")
    pol: Mapped[str] = mapped_column(String(128), default="")
    vessel: Mapped[str] = mapped_column(String(128), default="")
    carrier: Mapped[str] = mapped_column(String(128), default="")
    container_type: Mapped[str] = mapped_column(String(64), default="")
    cbm: Mapped[Optional[float]] = mapped_column(Float)
    # 机器柜 / 辅料柜：仅 Shipment List 页面手动编辑（不随导出写入、不进 .xlsm）
    machine_cabinet: Mapped[str] = mapped_column(String(64), default="")
    aux_cabinet: Mapped[str] = mapped_column(String(64), default="")
    ref_id: Mapped[str] = mapped_column(String(128), default="")
    invoice_ids: Mapped[str] = mapped_column(Text, default="[]")
    created_by: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


ensure_dirs()
engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    future=True,
)
SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def init_db() -> None:
    """建表。"""
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        conn.exec_driver_sql("PRAGMA busy_timeout=5000")
    Base.metadata.create_all(engine)
    _migrate_check_fields()
    _migrate_shipment_fields()


def backup_db(keep: int = 30) -> Optional[Path]:
    """每日启动备份 app.db 到 data/backups/，保留最近 keep 份。

    同一天只备份一次；返回本次备份路径，未备份（今日已有）返回 None。
    """
    import shutil
    from datetime import datetime as _dt

    backups = DB_PATH.parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    today = _dt.now().strftime("%Y%m%d")
    if any(backups.glob(f"app_{today}_*.db")):
        return None
    dest = backups / f"app_{today}_{_dt.now().strftime('%H%M%S')}.db"
    shutil.copy2(DB_PATH, dest)
    # 清理旧备份
    old = sorted(backups.glob("app_*.db"))[:-keep]
    for f in old:
        f.unlink(missing_ok=True)
    return dest


def _migrate_check_fields() -> None:
    """为已有库补上核对字段（SQLite ALTER ADD COLUMN）。"""
    with engine.begin() as conn:
        existing = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(invoices)").fetchall()
        }
        cols = [
            ("check_status", f"VARCHAR(16) DEFAULT '{CHECK_PENDING}'"),
            ("check_note", "TEXT DEFAULT ''"),
            ("checked_by", "VARCHAR(64)"),
            ("checked_at", "TIMESTAMP"),
            ("manual_values", "TEXT DEFAULT '{}'"),
            ("manual_saved_by", "VARCHAR(64)"),
            ("manual_saved_at", "TIMESTAMP"),
        ]
        for name, decl in cols:
            if name not in existing:
                conn.exec_driver_sql(f"ALTER TABLE invoices ADD COLUMN {name} {decl}")
        if existing and "check_status" in existing:
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_invoices_check_status "
                "ON invoices (check_status)"
            )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_invoices_uploaded_at "
            "ON invoices (uploaded_at)"
        )

    # export_file_versions 后补列（旧库迁移）
    with engine.begin() as conn:
        existing = {
            row[1]
            for row in conn.exec_driver_sql(
                "PRAGMA table_info(export_file_versions)"
            ).fetchall()
        }
        for name, decl in (
            ("form_values", "TEXT DEFAULT '{}'"),
            ("backup_path", "VARCHAR(512) DEFAULT ''"),
        ):
            if name not in existing:
                conn.exec_driver_sql(
                    f"ALTER TABLE export_file_versions ADD COLUMN {name} {decl}"
                )


def _migrate_shipment_fields() -> None:
    """为已有 shipment_list_entries 表补上柜型 / CBM / 机器柜 / 辅料柜 / source_key / ref_id 列（SQLite ALTER）。"""
    with engine.begin() as conn:
        existing = {
            row[1]
            for row in conn.exec_driver_sql(
                "PRAGMA table_info(shipment_list_entries)"
            ).fetchall()
        }
        for name, decl in (
            ("container_type", "VARCHAR(64) DEFAULT ''"),
            ("cbm", "FLOAT"),
            ("machine_cabinet", "VARCHAR(64) DEFAULT ''"),
            ("aux_cabinet", "VARCHAR(64) DEFAULT ''"),
            ("source_key", "VARCHAR(512) DEFAULT ''"),
            ("ref_id", "VARCHAR(128) DEFAULT ''"),
        ):
            if name not in existing:
                conn.exec_driver_sql(
                    f"ALTER TABLE shipment_list_entries ADD COLUMN {name} {decl}"
                )
        # 回填历史行的 source_key：从 fty_inv（源发票号空格连接）拆出并排序 + 工厂
        rows = conn.exec_driver_sql(
            "SELECT id, est_vco, fty_inv FROM shipment_list_entries "
            "WHERE source_key = '' OR source_key IS NULL"
        ).fetchall()
        for rid, est_vco, fty_inv in rows:
            nos = sorted((fty_inv or "").split())
            key = f"{est_vco or ''}:{','.join(nos)}" if nos else ""
            conn.exec_driver_sql(
                "UPDATE shipment_list_entries SET source_key = ? WHERE id = ?",
                (key, rid),
            )


def get_db():
    """FastAPI 依赖：数据库会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
