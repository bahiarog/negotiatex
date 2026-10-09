"""
Vorhaben (Customer Journey): die Klammer um Briefing, Dienstleistersuche,
Anfragen, Angebote, Verhandlung und Entscheidung. Ein Vorhaben orchestriert
die bestehenden Module (SourcingRequest, Kandidaten/Outreach, NDA, RFQ,
Angebote, Teil-A-Verhandlung, Master Data Center) und fuehrt einen fuer den
Kunden verstaendlichen Zeitstrahl (ProjectEvent).

Zwei Einstiege:
  new_need       -- Kunde beschreibt den Bedarf (Eingabe) oder laedt ein Briefing hoch
  existing_offer -- Kunde hat bereits ein Angebot; der Agent prueft, verhandelt
                    und holt Vergleichsangebote ein
"""
import enum
import uuid

from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, JSON, Numeric, String, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID

from database import Base


class ProjectStatus(str, enum.Enum):
    briefing = "briefing"                    # Entwurf, Kunde ergaenzt/bestaetigt
    sourcing = "sourcing"                    # Agent sucht und kontaktiert Dienstleister
    collecting_offers = "collecting_offers"  # Anfragen raus, Angebote laufen ein
    evaluating = "evaluating"                # Angebote pruefen, verhandeln
    decision = "decision"                    # Kunde entscheidet
    awarded = "awarded"
    cancelled = "cancelled"


class Project(Base):
    __tablename__ = "projects"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    entry_path = Column(String(30), nullable=False)          # new_need | existing_offer
    status = Column(SAEnum(ProjectStatus, name="project_status"), default=ProjectStatus.briefing, nullable=False)

    # Briefing (vom Kunden bestaetigt; KI-Vorschlag nur als Ausgangspunkt)
    service_type = Column(String(255), nullable=True)
    description = Column(Text, nullable=True)
    must_criteria_json = Column(JSON, default=dict)
    conditions_text = Column(Text, nullable=True)            # z.B. Zahlungsziel, Nutzungsrechte, Ort
    budget_target = Column(Numeric(18, 2), nullable=True)
    budget_ceiling = Column(Numeric(18, 2), nullable=True)
    currency = Column(String(10), default="EUR")
    needed_by = Column(Date, nullable=True)
    region = Column(String(100), nullable=True)
    delivery_location = Column(String(255), nullable=True)
    quantity = Column(Numeric(18, 4), nullable=True)
    unit = Column(String(50), nullable=True)
    briefing_source = Column(String(20), nullable=True)      # prompt | document | offer
    briefing_input = Column(Text, nullable=True)             # Original-Eingabe bzw. Dokumenttext (gekuerzt)
    briefing_document_id = Column(UUID(as_uuid=True), ForeignKey("mdc_documents.id", ondelete="SET NULL"), nullable=True)
    open_questions_json = Column(JSON, default=list)
    category_id = Column(UUID(as_uuid=True), ForeignKey("mdc_categories.id", ondelete="SET NULL"), nullable=True)

    # Verknuepfung zu den bestehenden Modulen
    sourcing_request_id = Column(UUID(as_uuid=True), ForeignKey("sourcing_requests.id", ondelete="SET NULL"), nullable=True, index=True)
    rfq_id = Column(UUID(as_uuid=True), ForeignKey("rfqs.id", ondelete="SET NULL"), nullable=True, index=True)
    incumbent_candidate_id = Column(UUID(as_uuid=True), ForeignKey("supplier_candidates.id", ondelete="SET NULL"), nullable=True)
    seek_alternatives = Column(Boolean, default=True, nullable=False)
    negotiations_json = Column(JSON, default=dict)           # {offer_id: case_id}

    customer_email = Column(String(255), nullable=True)
    notify_milestones = Column(Boolean, default=True, nullable=False)

    awarded_offer_id = Column(UUID(as_uuid=True), ForeignKey("rfq_offers.id", ondelete="SET NULL"), nullable=True)
    awarded_at = Column(DateTime, nullable=True)
    awarded_by = Column(String(100), nullable=True)
    decision_note = Column(Text, nullable=True)

    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class ProjectEvent(Base):
    """Kundenverstaendlicher Zeitstrahl. Meilensteine (milestone=True) werden
    zusaetzlich per E-Mail an customer_email gemeldet."""
    __tablename__ = "project_events"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = Column(String(50), nullable=False)
    title = Column(String(255), nullable=False)
    detail = Column(Text, nullable=True)
    actor = Column(String(100), nullable=False, default="agent")
    milestone = Column(Boolean, default=False, nullable=False)
    emailed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), index=True)


class OfferReview(Base):
    """AGB-/Compliance-Pruefung eines Angebots. Jede Feststellung mit
    woertlichem, im Angebot nachgewiesenem Zitat oder als deterministische
    Regel; Ergebnis ist ein Hinweis fuer den Kunden, keine Entscheidung."""
    __tablename__ = "offer_reviews"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    offer_id = Column(UUID(as_uuid=True), ForeignKey("rfq_offers.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    status = Column(String(20), nullable=False)              # ok | warning | critical | not_checked
    findings_json = Column(JSON, default=list)
    offer_text_excerpt = Column(Text, nullable=True)
    rules_version = Column(String(20), nullable=False)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
