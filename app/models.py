import uuid, enum
from datetime import datetime
from sqlalchemy import Boolean, Column, String, Integer, Float, Text, DateTime, ForeignKey, JSON, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base

class OfferStatus(str, enum.Enum):
    uploaded="uploaded"; analyzing="analyzing"; analyzed="analyzed"; po_created="po_created"

class POStatus(str, enum.Enum):
    draft="draft"; sent="sent"; confirmed="confirmed"; completed="completed"; cancelled="cancelled"

class CheckType(str, enum.Enum):
    pricing="pricing"; savings="savings"; terms="terms"; general="general"

class Client(Base):
    __tablename__ = "clients"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_name = Column(String(255), nullable=False)
    contact_name = Column(String(255))
    email = Column(String(255), unique=True, nullable=False)
    industry = Column(String(100))
    annual_volume = Column(Float)
    pricing_model = Column(String(20), default="success_fee")
    notes = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
    audits = relationship("Audit", back_populates="client", cascade="all, delete-orphan")
    suppliers = relationship("Supplier", back_populates="client", cascade="all, delete-orphan")

class Supplier(Base):
    __tablename__ = "suppliers"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id = Column(UUID(as_uuid=True), ForeignKey("clients.id", ondelete="CASCADE"))
    owner_user_id = Column(UUID(as_uuid=True), nullable=True)  # FK to auth users.id (loose, no DB constraint — see auth/tenant note)
    name = Column(String(255), nullable=False)
    legal_form = Column(String(50))            # GmbH, AG, UG, Einzelunternehmen, etc.
    category = Column(String(100))
    contact_name = Column(String(255))
    phone = Column(String(50))
    email = Column(String(255))
    website = Column(String(255))
    address = Column(Text)
    country = Column(String(100), default="Germany")
    tax_id = Column(String(100))               # Steuernummer
    vat_id = Column(String(50))                # USt-IdNr.
    duns_number = Column(String(20))           # D-U-N-S Nummer
    commercial_register_number = Column(String(100))  # Handelsregisternummer (z.B. HRB 12345)
    employee_count = Column(Integer)
    iban = Column(String(50))
    bic = Column(String(20))
    bank_name = Column(String(255))
    withholding_tax_liable = Column(Boolean, default=False)  # Quellensteuerpflichtig
    payment_terms_days = Column(Integer)
    notes = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
    client = relationship("Client", back_populates="suppliers")
    offers = relationship("Offer", back_populates="supplier")
    purchase_orders = relationship("PurchaseOrder", back_populates="supplier")

class Audit(Base):
    __tablename__ = "audits"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id = Column(UUID(as_uuid=True), ForeignKey("clients.id", ondelete="CASCADE"))
    status = Column(String(50), default="pending")
    questionnaire = Column(JSON)
    ai_summary = Column(JSON)
    total_savings_identified = Column(Float, default=0)
    savings_percentage = Column(Float, default=0)
    pdf_report_path = Column(String(500))
    created_at = Column(DateTime, server_default=func.now())
    completed_at = Column(DateTime)
    client = relationship("Client", back_populates="audits")
    offers = relationship("Offer", back_populates="audit", cascade="all, delete-orphan")
    activity_log = relationship("ActivityLog", back_populates="audit", cascade="all, delete-orphan")

class Offer(Base):
    __tablename__ = "offers"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    audit_id = Column(UUID(as_uuid=True), ForeignKey("audits.id", ondelete="CASCADE"))
    supplier_id = Column(UUID(as_uuid=True), ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True)
    title = Column(String(255))
    category = Column(String(100))
    job_number = Column(String(100))
    total_net = Column(Float, default=0)
    currency = Column(String(10), default="EUR")
    pdf_file_path = Column(String(500))
    pdf_file_name = Column(String(255))
    pdf_file_size = Column(Integer)
    parsed_text = Column(Text)
    status = Column(SAEnum(OfferStatus), default=OfferStatus.uploaded)
    ai_score = Column(Integer)
    ai_analysis = Column(JSON)
    created_at = Column(DateTime, server_default=func.now())
    analyzed_at = Column(DateTime)
    audit = relationship("Audit", back_populates="offers")
    supplier = relationship("Supplier", back_populates="offers")
    ai_checks = relationship("AICheck", back_populates="offer", cascade="all, delete-orphan")
    purchase_orders = relationship("PurchaseOrder", back_populates="offer")

class AICheck(Base):
    __tablename__ = "ai_checks"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    offer_id = Column(UUID(as_uuid=True), ForeignKey("offers.id", ondelete="CASCADE"))
    check_type = Column(SAEnum(CheckType), nullable=False)
    score = Column(Integer, default=0)
    result = Column(JSON)
    critical_count = Column(Integer, default=0)
    warning_count = Column(Integer, default=0)
    ok_count = Column(Integer, default=0)
    raw_response = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
    offer = relationship("Offer", back_populates="ai_checks")

class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    po_number = Column(String(100), unique=True, nullable=False)
    offer_id = Column(UUID(as_uuid=True), ForeignKey("offers.id", ondelete="SET NULL"), nullable=True)
    supplier_id = Column(UUID(as_uuid=True), ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True)
    audit_id = Column(UUID(as_uuid=True), ForeignKey("audits.id", ondelete="SET NULL"), nullable=True)
    job_number = Column(String(100))
    amount_net = Column(Float, default=0)
    amount_gross = Column(Float, default=0)
    tax_rate = Column(Float, default=19.0)
    cost_center = Column(String(100))
    orderer_name = Column(String(255))
    orderer_email = Column(String(255))
    delivery_date = Column(DateTime)
    notes = Column(Text)
    status = Column(SAEnum(POStatus), default=POStatus.draft)
    pdf_generated_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now())
    offer = relationship("Offer", back_populates="purchase_orders")
    supplier = relationship("Supplier", back_populates="purchase_orders")
    items = relationship("POItem", back_populates="purchase_order", cascade="all, delete-orphan")

class POItem(Base):
    __tablename__ = "po_items"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    purchase_order_id = Column(UUID(as_uuid=True), ForeignKey("purchase_orders.id", ondelete="CASCADE"))
    position_nr = Column(Integer, default=1)
    description = Column(Text, nullable=False)
    quantity = Column(Float, default=1)
    unit = Column(String(50), default="Lump sum")
    unit_price = Column(Float, default=0)
    total_price = Column(Float, default=0)
    purchase_order = relationship("PurchaseOrder", back_populates="items")

class ActivityLog(Base):
    __tablename__ = "activity_log"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    audit_id = Column(UUID(as_uuid=True), ForeignKey("audits.id", ondelete="CASCADE"), nullable=True)
    entity_type = Column(String(50))
    entity_id = Column(String(100))
    action = Column(String(100))
    message = Column(Text)
    user_name = Column(String(255))
    created_at = Column(DateTime, server_default=func.now())
    audit = relationship("Audit", back_populates="activity_log")


class BenchmarkEntry(Base):
    __tablename__ = "benchmark_entries"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    category = Column(String(100), nullable=False)        # Software & IT, Marketing, etc.
    metric_type = Column(String(50), nullable=False)      # hourly_rate, daily_rate, fte, flat
    metric_label = Column(String(255))                    # "Senior Developer", "Project Manager"
    value = Column(Float, nullable=False)                 # e.g. 115.0
    currency = Column(String(10), default="EUR")
    region = Column(String(100), default="DACH")
    source = Column(String(50), default="offer")          # offer | manual | market_data
    offer_id = Column(UUID(as_uuid=True), ForeignKey("offers.id", ondelete="SET NULL"), nullable=True)
    audit_id = Column(UUID(as_uuid=True), ForeignKey("audits.id", ondelete="SET NULL"), nullable=True)
    supplier_anonymized = Column(String(100))             # first 3 chars only
    verified = Column(Boolean, default=False)             # admin verified
    notes = Column(Text)
    created_at = Column(DateTime, server_default=func.now())

class SupplierInvite(Base):
    __tablename__ = "supplier_invites"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token_hash = Column(String(64), nullable=False, unique=True)
    email = Column(String(255), nullable=False)
    name_hint = Column(String(255))
    status = Column(String(20), default="pending")  # pending | completed
    supplier_id = Column(UUID(as_uuid=True), ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True)
    confirmed_by_name = Column(String(255))
    confirmed_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now())


class APIKey(Base):
    __tablename__ = "api_keys"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=False)  # FK to auth user
    name = Column(String(100), nullable=False)  # e.g. "My Integration"
    key_hash = Column(String(255), nullable=False, unique=True)  # SHA256 of the key
    key_prefix = Column(String(12), nullable=False)  # first 8 chars for display (e.g. "ntx_xxxx")
    is_active = Column(Boolean, default=True)
    last_used = Column(DateTime, nullable=True)
    requests_today = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())
