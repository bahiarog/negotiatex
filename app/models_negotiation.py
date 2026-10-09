"""
Teil A (CTO-Playbook, "Agenten-Playbook" Seiten 3-8): Verhandlungs-Workflow
mit zwei Preisrunden, menschlicher Freigabe vor jedem Versand und
hash-gebundener Freigabe-Integritaet.

Additive zu models_v2.py -- rueht keine bestehende Tabelle an. Alle
Geldbetraege als Numeric/Decimal, nie Float (gleiche Konvention wie
models_v2.LineItem).
"""
import uuid
import enum
from sqlalchemy import (
    Column, String, Integer, Numeric, Text, DateTime, Boolean,
    ForeignKey, JSON, Enum as SAEnum, UniqueConstraint
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from database import Base


class NegotiationActionType(str, enum.Enum):
    propose_price = "propose_price"
    send_reminder = "send_reminder"


class NegotiationActionStatus(str, enum.Enum):
    draft = "draft"
    pending_approval = "pending_approval"
    approved = "approved"
    sent = "sent"
    rejected = "rejected"
    superseded = "superseded"


class EmailDirection(str, enum.Enum):
    outbound = "outbound"
    inbound = "inbound"


class NegotiationExceptionType(str, enum.Enum):
    scope_change = "scope_change"
    new_commitment = "new_commitment"
    clarifying_question = "clarifying_question"
    no_movement = "no_movement"
    no_reply = "no_reply"
    prompt_injection_attempt = "prompt_injection_attempt"
    unknown_sender = "unknown_sender"


class NegotiationStrategy(Base):
    """Per-case Verhandlungsstrategie. Vom Menschen bestaetigt, bevor
    irgendeine ausgehende Aktion moeglich ist ("Rafael bestaetigt
    Ausgangslage und Regeln"). Leistungsumfang/Nutzungsrechte/Liefertermin
    sind unveraenderlicher Freitext -- werden nie neu verhandelt, nur
    unveraendert in jede E-Mail uebernommen und gegen Lieferantenantworten
    auf Abweichung geprueft."""
    __tablename__ = "negotiation_strategies"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    case_id = Column(UUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)

    starting_price_net = Column(Numeric(18, 4), nullable=False)
    round1_price_net = Column(Numeric(18, 4), nullable=False)
    round2_price_net = Column(Numeric(18, 4), nullable=False)
    price_cap_net = Column(Numeric(18, 4), nullable=False)  # harte Obergrenze, nie ueberschritten
    currency = Column(String(10), default="EUR", nullable=False)

    scope_text = Column(Text, nullable=False)            # Leistungsumfang, unveraenderlich
    usage_rights_text = Column(Text, nullable=False)      # Nutzungsrechte, unveraenderlich
    delivery_date = Column(String(100), nullable=False)   # Liefertermin, unveraenderlich

    max_rounds = Column(Integer, default=2, nullable=False)
    supplier_email = Column(String(255), nullable=False)  # einzige akzeptierte Absenderadresse fuer Antworten

    created_by = Column(String(100), nullable=False)
    created_at = Column(DateTime, server_default=func.now())


class NegotiationAction(Base):
    """Eine einzelne ausgehende Aktion (Preisvorschlag oder Erinnerung).
    Preis wird NIE vom LLM gewaehlt -- immer deterministisch aus der
    Strategie (round1_price_net / round2_price_net) abgeleitet."""
    __tablename__ = "negotiation_actions"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id = Column(UUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)

    round_number = Column(Integer, nullable=False)
    action_type = Column(SAEnum(NegotiationActionType, name="negotiation_action_type"), nullable=False)
    proposed_price_net = Column(Numeric(18, 4), nullable=True)
    currency = Column(String(10), default="EUR")
    scope_change = Column(Boolean, default=False, nullable=False)
    reason = Column(Text)
    open_questions = Column(JSON, default=list)

    recipient_email = Column(String(255), nullable=False)
    rendered_subject = Column(Text)
    rendered_body = Column(Text)

    payload_hash = Column(String(64), nullable=True)  # sha256, gesetzt erst bei Freigabe
    status = Column(SAEnum(NegotiationActionStatus, name="negotiation_action_status"),
                     default=NegotiationActionStatus.draft, nullable=False)

    case_version_at_draft = Column(Integer, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class NegotiationApproval(Base):
    """Das 'unabhaengige Policy-Service'-Freigabeobjekt. payload_hash ist
    der Hash, der TATSAECHLICH freigegeben wurde -- vor dem Versand wird
    der Hash aus dem tatsaechlich zu sendenden Inhalt neu berechnet und mit
    diesem Feld verglichen. Jede Abweichung blockiert den Versand (400)."""
    __tablename__ = "negotiation_approvals"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    action_id = Column(UUID(as_uuid=True), ForeignKey("negotiation_actions.id", ondelete="CASCADE"), nullable=False, index=True)
    approved_by = Column(String(100), nullable=False)
    payload_hash = Column(String(64), nullable=False)
    approved_at = Column(DateTime, server_default=func.now())
    revoked_at = Column(DateTime, nullable=True)


class EmailMessage(Base):
    """Audit-Trail jeder ein-/ausgehenden E-Mail. message_id ist UNIQUE ->
    macht wiederholtes IMAP-Polling derselben Nachricht idempotent (kein
    doppeltes Verarbeiten)."""
    __tablename__ = "negotiation_email_messages"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id = Column(UUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)

    direction = Column(SAEnum(EmailDirection, name="negotiation_email_direction"), nullable=False)
    message_id = Column(String(998), nullable=False)
    in_reply_to = Column(String(998), nullable=True)
    references_header = Column(Text, nullable=True)

    from_addr = Column(String(255), nullable=False)
    to_addr = Column(String(255), nullable=False)
    subject = Column(Text)
    body_text = Column(Text)
    raw_source = Column(Text)

    occurred_at = Column(DateTime, server_default=func.now())

    __table_args__ = (UniqueConstraint("message_id", name="uq_negotiation_email_message_id"),)


class NegotiationException(Base):
    """Jede A5-Ausnahme landet hier -- nie automatisch aufgeloest. Ein
    offener Eintrag (resolved=False) blockiert den naechsten automatischen
    Schritt und erscheint im Dashboard-Banner."""
    __tablename__ = "negotiation_exceptions"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id = Column(UUID(as_uuid=True), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)

    exception_type = Column(SAEnum(NegotiationExceptionType, name="negotiation_exception_type"), nullable=False)
    detail_text = Column(Text)
    source_email_message_id = Column(UUID(as_uuid=True), ForeignKey("negotiation_email_messages.id", ondelete="SET NULL"), nullable=True)

    detected_at = Column(DateTime, server_default=func.now())
    resolved = Column(Boolean, default=False, nullable=False)
    resolution_note = Column(Text, nullable=True)
    resolved_by = Column(String(100), nullable=True)
