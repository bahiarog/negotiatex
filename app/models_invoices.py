import uuid
from sqlalchemy import Column, String, Text, DateTime, ForeignKey, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from database import Base

# Invoice status: received -> extracted -> matched | exception -> approved_for_payment -> paid
# (or rejected at any point). Matching here is strictly 2-way (invoice vs. PO) --
# this system has no goods-receipt / delivery-confirmation concept yet, so a
# true 3-way match is not possible and is not claimed.


class Invoice(Base):
    __tablename__ = "invoices"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    supplier_id = Column(UUID(as_uuid=True), ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True)
    invoice_number = Column(String(100), nullable=True)
    invoice_date = Column(DateTime, nullable=True)
    due_date = Column(DateTime, nullable=True)
    currency = Column(String(10), default="EUR")
    total_net = Column(Numeric(14, 2), nullable=True)
    total_tax = Column(Numeric(14, 2), nullable=True)
    total_gross = Column(Numeric(14, 2), nullable=True)
    purchase_order_id = Column(UUID(as_uuid=True), ForeignKey("purchase_orders.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(30), default="received")
    file_path = Column(String(500), nullable=True)
    file_name = Column(String(255), nullable=True)
    parsed_text = Column(Text, nullable=True)
    uploaded_at = Column(DateTime, server_default=func.now())


class InvoiceLineItem(Base):
    __tablename__ = "invoice_line_items"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    invoice_id = Column(UUID(as_uuid=True), ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False)
    position_nr = Column(Numeric(6, 0), default=1)
    description = Column(Text, nullable=False)
    quantity = Column(Numeric(14, 3), nullable=True)
    unit_price = Column(Numeric(14, 2), nullable=True)
    net_total = Column(Numeric(14, 2), nullable=True)
    matched_po_item_id = Column(UUID(as_uuid=True), ForeignKey("po_items.id", ondelete="SET NULL"), nullable=True)
    match_status = Column(String(20), default="unmatched")  # unmatched | ok | price_variance | qty_variance
