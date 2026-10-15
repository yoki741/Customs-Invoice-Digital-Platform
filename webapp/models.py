# -*- coding: utf-8 -*-
"""SQLAlchemy 模型与数据库会话。"""

from __future__ import annotations

from datetime import datetime
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
ROLE_LABELS = {
    ROLE_ADMIN: "管理员",
    ROLE_TYPE1: "第一类（业务/关务）",
    ROLE_TYPE2: "第二类（明细查看）",
}


# 核对状态（第二类账号核对，第一类账号修复）
CHECK_PENDING = "pending"
CHECK_PASSED = "passed"
CHECK_FAILED = "failed"
CHECK_LABELS = {
    CHECK_PENDING: "待核对",
    CHECK_PASSED: "核对通过",
    CHECK_FAILED: "有问题",
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
    Base.metadata.create_all(engine)
    _migrate_check_fields()


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
        ]
        for name, decl in cols:
            if name not in existing:
                conn.exec_driver_sql(f"ALTER TABLE invoices ADD COLUMN {name} {decl}")
        if existing and "check_status" in existing:
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_invoices_check_status "
                "ON invoices (check_status)"
            )


def get_db():
    """FastAPI 依赖：数据库会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
