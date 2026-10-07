import uuid
from sqlalchemy import Column, String, Integer, Text, DateTime, ForeignKey, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from database import Base

# Requisition status enum (kept as plain string, not SAEnum, so new statuses can be
# added without an ALTER TYPE migration):
# draft -> submitted -> pending_approval -> approved | rejected -> converted_to_po
# (or cancelled at any point before a terminal state)


class Requisition(Base):
    __tablename__ = "requisitions"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    requester_name = Column(String(255), nullable=False)
    requester_email = Column(String(255), nullable=False)
    title = Column(String(255), nullable=False)
    justification = Column(Text)
    category = Column(String(100))
    supplier_id = Column(UUID(as_uuid=True), ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True)
    currency = Column(String(10), default="EUR")
    estimated_amount = Column(Numeric(14, 2), default=0)
    status = Column(String(30), default="draft")
    purchase_order_id = Column(UUID(as_uuid=True), ForeignKey("purchase_orders.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    decided_at = Column(DateTime, nullable=True)


class RequisitionItem(Base):
    __tablename__ = "requisition_items"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    requisition_id = Column(UUID(as_uuid=True), ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False)
    description = Column(Text, nullable=False)
    quantity = Column(Numeric(14, 3), default=1)
    unit = Column(String(50), default="Stk.")
    unit_price = Column(Numeric(14, 2), default=0)
    total = Column(Numeric(14, 2), default=0)


class RequisitionApproval(Base):
    __tablename__ = "requisition_approvals"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    requisition_id = Column(UUID(as_uuid=True), ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False)
    level = Column(Integer, nullable=False)  # 1-based
    approver_name = Column(String(255), nullable=False)
    approver_email = Column(String(255), nullable=False)
    status = Column(String(20), default="pending")  # pending | approved | rejected
    comment = Column(Text, nullable=True)
    approval_token_hash = Column(String(64), nullable=True, unique=True)
    decided_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
