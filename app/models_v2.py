"""
Phase 1 data model (CTO briefing, 7.10.2026): tenants, documents, extraction,
policy engine primitives, case state machine, savings tracking.

Additive only -- does not touch or rename any table in models.py. The legacy
`clients`/`suppliers`/`offers`/`purchase_orders` tables keep working unchanged
for anything that already depends on them.

Multi-tenancy note (Phase 1 scope): tenant isolation is enforced at the
application/query layer only (every router dependency resolves tenant_id from
the authenticated user's `memberships` row, never from client input, and every
query is filtered by it). Postgres Row-Level-Security (RLS policies) is a
Phase 2 hardening item -- deliberately NOT implemented here.
"""
import uuid
import enum
from decimal import Decimal

from sqlalchemy import (
    Column, String, Integer, Numeric, Text, DateTime, Boolean, Float,
    ForeignKey, JSON, Enum as SAEnum, UniqueConstraint
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from database import Base


class MembershipRole(str, enum.Enum):
    owner = "owner"
    member = "member"


class CaseStatus(str, enum.Enum):
    """Exactly the 14 states from the CTO briefing (page 4). Phase 1 drives
    transitions synchronously inside API handlers -- there is no running
    workflow engine yet (that is Phase 2+)."""
    RECEIVED = "RECEIVED"
    EXTRACTING = "EXTRACTING"
    NEEDS_DATA = "NEEDS_DATA"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    READY_TO_DRAFT = "READY_TO_DRAFT"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    SEND_PENDING = "SEND_PENDING"
    WAITING_SUPPLIER = "WAITING_SUPPLIER"
    EVALUATING_RESPONSE = "EVALUATING_RESPONSE"
    READY_FOR_DECISION = "READY_FOR_DECISION"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"
    PAUSED = "PAUSED"


class PolicyCheckResult(str, enum.Enum):
    ok = "ok"
    warning = "warning"
    violation = "violation"


class Tenant(Base):
    __tablename__ = "tenants"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_name = Column(String(255), nullable=False)
    # Erfolgsgebuehr in % der Einsparung; NULL = nicht vereinbart ("noch nicht ermittelt")
    success_fee_pct = Column(Numeric(5, 2), nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class Membership(Base):
    """Missing link between login (`User` in routers/auth.py) and tenant
    data. A user with no membership row has no tenant access (403), never a
    default tenant."""
    __tablename__ = "memberships"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(SAEnum(MembershipRole), default=MembershipRole.member, nullable=False)
    # Rollen im Sinne von access.py (customer/procurement/admin); leer = aus `role` abgeleitet
    roles_json = Column(JSON, nullable=True)
    # Entscheidungsbefugnis (Agent starten, Mandat, Zuschlag) -- unabhaengig von Admin
    can_decide = Column(Boolean, default=False, nullable=False, server_default="false")
    created_at = Column(DateTime, server_default=func.now())
    __table_args__ = (UniqueConstraint("tenant_id", "user_id", name="uq_membership_tenant_user"),)


class SupplierV2(Base):
    """Tenant-scoped supplier record. Deliberately separate from the legacy
    global `suppliers` table -- that one is untouched.

    Teil B (B5 Stammblatt) extension, 9.10.2026: added the rich German B2B
    fields that the legacy (non-tenant-scoped) `models.Supplier` table
    already had, since that is clearly the desired target schema -- just
    applied here, tenant-scoped. Added via manual `ALTER TABLE` on the
    running DB (see deployment notes) because `Base.metadata.create_all()`
    never alters an existing table. Bank fields: `bank_data_verified` is
    reset to False by routers/sourcing.py `update_bank_data` any time
    iban/bic actually changes -- "eine Aenderung von Bankdaten loest eine
    unabhaengige Verifikation aus", implemented literally."""
    __tablename__ = "suppliers_v2"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    category = Column(String(100))
    contact_name = Column(String(255))
    email = Column(String(255))
    address = Column(Text)
    country = Column(String(100))
    tax_id = Column(String(100))
    created_at = Column(DateTime, server_default=func.now())

    # -- Teil B / B5 additions --
    legal_form = Column(String(50))
    vat_id = Column(String(50))
    commercial_register_number = Column(String(100))
    employee_count = Column(Integer)
    capacity_notes = Column(Text)
    payment_terms_days = Column(Integer)
    iban = Column(String(50))
    bic = Column(String(20))
    bank_name = Column(String(255))
    bank_data_verified = Column(Boolean, default=False)
    bank_data_verified_at = Column(DateTime, nullable=True)


class Case(Base):
    __tablename__ = "cases"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    category = Column(String(100))
    status = Column(SAEnum(CaseStatus, name="case_status"), default=CaseStatus.RECEIVED, nullable=False)
    case_version = Column(Integer, default=1, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class CaseEvent(Base):
    """Append-only audit trail of every status transition. This is the
    Phase 1 'events' table -- the full events+outbox delivery-worker pattern
    is Phase 2 (outbox/delivery is deliberately not built here)."""
    __tablename__ = "case_events"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id = Column(UUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    from_status = Column(String(50), nullable=True)
    to_status = Column(String(50), nullable=False)
    actor = Column(String(100), nullable=False)  # user id (str) or "system"
    reason = Column(Text)
    created_at = Column(DateTime, server_default=func.now())


class Document(Base):
    __tablename__ = "documents"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    case_id = Column(UUID(as_uuid=True), ForeignKey("cases.id", ondelete="SET NULL"), nullable=True, index=True)
    object_path = Column(String(1000), nullable=False)
    file_hash = Column(String(64), nullable=False)  # sha256
    original_filename = Column(String(500))
    mime_type = Column(String(200))
    version = Column(Integer, default=1)
    uploaded_at = Column(DateTime, server_default=func.now())


class LineItem(Base):
    """Structured fields per briefing: Menge, Einheit, Einzelpreis,
    Nettosumme, Steuerkennzeichnung, Waehrung, Laufzeit, Zahlungsziel,
    Leistungsumfang, Kuendigungsfrist. Missing values stay NULL -- never
    fabricated/guessed. Money fields use Numeric (Decimal), not Float,
    per the briefing's explicit "niemals unkontrollierte Fliesskommazahlen"
    requirement (unlike the legacy po_items/offers tables, which still use
    Float and are intentionally left alone)."""
    __tablename__ = "line_items"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    position_nr = Column(Integer)
    description = Column(Text)
    quantity = Column(Numeric(18, 4))
    unit = Column(String(50))
    unit_price = Column(Numeric(18, 4))
    net_total = Column(Numeric(18, 4))
    currency = Column(String(10))
    tax_rate = Column(Numeric(6, 3))
    contract_period = Column(String(255))       # Laufzeit (free text, Phase 1)
    cancellation_notice = Column(String(255))    # Kuendigungsfrist (free text, Phase 1)
    raw_extracted_json = Column(JSON)
    confidence = Column(Float, nullable=True)    # extractor's own confidence, not a money value
    created_at = Column(DateTime, server_default=func.now())


class Policy(Base):
    """Versioned policy records, CRUD'd by the tenant (not by any agent)."""
    __tablename__ = "policies"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    version = Column(Integer, default=1, nullable=False)
    is_active = Column(Boolean, default=True)
    rules_json = Column(JSON, nullable=False)
    created_at = Column(DateTime, server_default=func.now())


class PolicyCheck(Base):
    """Deterministic rule-evaluation output ("Alignment" role). Always
    references which policy_version was used."""
    __tablename__ = "policy_checks"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id = Column(UUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    policy_id = Column(UUID(as_uuid=True), ForeignKey("policies.id", ondelete="SET NULL"), nullable=True)
    policy_version = Column(Integer)
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True)
    line_item_id = Column(UUID(as_uuid=True), ForeignKey("line_items.id", ondelete="SET NULL"), nullable=True)
    result = Column(SAEnum(PolicyCheckResult, name="policy_check_result"), nullable=False)
    message = Column(Text)
    created_at = Column(DateTime, server_default=func.now())


class SavingsRecord(Base):
    """Three-way split per briefing: potential / agreed / realized. Phase 1
    only ever populates potential_amount (via the MVP value function in
    services/policy_engine.py). agreed_amount/realized_amount stay NULL
    until Phase 2/3 wire up approvals and invoice reconciliation."""
    __tablename__ = "savings_records"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    case_id = Column(UUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True)
    category = Column(String(100))
    potential_amount = Column(Numeric(18, 2), nullable=False)
    agreed_amount = Column(Numeric(18, 2), nullable=True)
    realized_amount = Column(Numeric(18, 2), nullable=True)
    currency = Column(String(10), default="EUR")
    period_start = Column(DateTime, nullable=True)
    period_end = Column(DateTime, nullable=True)
    evidence_document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
