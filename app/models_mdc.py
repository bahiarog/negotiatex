"""
Master Data Center -- Etappe 1 (Datenkern) + Etappe 2 (pruefbare Preise).

Etappe 1: Kategorie/Spezifikation, Lieferanten-Identitaet, Originaldokumente
und versionierte Dokumentfassungen mit Beleganker (Seiten-/Tabellentext aus
services/pdf_parser).

Etappe 2: MDCLineItem -- strukturierte Preispositionen je Dokumentversion.
field_evidence/review aus der Anleitung (Abschnitt 05) ist hier bewusst
NICHT als eigene Tabelle umgesetzt, sondern als source_evidence/open_issues
je Zeile vereinfacht (MVP-Entscheidung, siehe Bericht) -- eine separate
Feld-fuer-Feld-Historie mit Transformationsschritten kann spaeter ergaenzt
werden, ohne das Grundschema zu aendern.

Bewusst NICHT Teil von Etappe 1/2 (folgt in Etappe 3):
  - retrieval_chunk (pgvector-Embeddings)
  - analysis_snapshot (Vergleichsergebnisse, Agenten-Tools wie compare_offer)
"""
import enum
import uuid

from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, Integer, JSON, Numeric, String, Text, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID

from database import Base


class MDCImportStatus(str, enum.Enum):
    uploaded = "uploaded"
    parsed = "parsed"
    extracted = "extracted"
    validated = "validated"
    needs_review = "needs_review"
    approved = "approved"
    rejected = "rejected"
    superseded = "superseded"
    indexed = "indexed"


class MDCDocumentType(str, enum.Enum):
    ratecard = "ratecard"
    price_list = "price_list"
    offer = "offer"
    contract = "contract"
    invoice = "invoice"
    other = "other"


class MDCTaxBasis(str, enum.Enum):
    net = "net"
    gross = "gross"
    unknown = "unknown"


class MDCAncillaryCostStatus(str, enum.Enum):
    inclusive = "inclusive"
    exclusive = "exclusive"
    unknown = "unknown"


class MDCPriceStatus(str, enum.Enum):
    """Anleitung Abschnitt 06 'Status': list_price, quoted, negotiated_quote,
    contracted, invoiced -- getrennt vom Pruef-/Freigabestatus review_status."""
    list_price = "list_price"
    quoted = "quoted"
    negotiated_quote = "negotiated_quote"
    contracted = "contracted"
    invoiced = "invoiced"


class MDCReviewStatus(str, enum.Enum):
    """Qualitaetsstatus (Anleitung Abschnitt 06): EXTRACTED -> NEEDS_REVIEW
    oder direkt -> APPROVED/REJECTED. Nur APPROVED fliesst in automatisierte
    Preisvergleiche ein (Etappe 3)."""
    extracted = "extracted"
    needs_review = "needs_review"
    approved = "approved"
    rejected = "rejected"
    superseded = "superseded"


class MDCCategory(Base):
    """Kategorie/Spezifikation (Anleitung Abschnitt 05). Jede Kategorie hat
    eigene Pflichtmerkmale und damit eigene Vergleichsregeln -- 'Video-
    Buyouts und Verpackungsstaffeln sind unterschiedliche Modelle'."""
    __tablename__ = "mdc_categories"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    version = Column(Integer, default=1, nullable=False)
    must_criteria_json = Column(JSON, default=dict)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class MDCSupplier(Base):
    """Bestaetigte Lieferanten-Identitaet fuer den Datenpool -- bewusst
    getrennt von SupplierCandidate (Sourcing) und SupplierV2 (Einkauf):
    hier geht es um Preis-Herkunft, nicht um einen laufenden
    Beschaffungsvorgang. 'Keine automatische Zusammenfuehrung nur nach
    aehnlichem Namen' -- confirmed bleibt menschlich gesetzt."""
    __tablename__ = "mdc_suppliers"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    domain = Column(String(255), nullable=True)
    aliases_json = Column(JSON, default=list)
    confirmed = Column(Boolean, default=False, nullable=False)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class MDCDocument(Base):
    """Fachlicher Vorgang mit stabiler ID. Revisionen sind Versionen
    desselben Dokuments (MDCDocumentVersion), nicht eigene Dokumente -- 'eine
    neue Revision erzeugt keinen zusaetzlichen unabhaengigen Marktbeleg'."""
    __tablename__ = "mdc_documents"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    category_id = Column(UUID(as_uuid=True), ForeignKey("mdc_categories.id", ondelete="SET NULL"), nullable=True, index=True)
    supplier_id = Column(UUID(as_uuid=True), ForeignKey("mdc_suppliers.id", ondelete="SET NULL"), nullable=True, index=True)
    document_type = Column(SAEnum(MDCDocumentType, name="mdc_document_type"), default=MDCDocumentType.other, nullable=False)
    title = Column(String(255), nullable=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class MDCDocumentVersion(Base):
    """Eine konkrete hochgeladene Fassung. file_hash + tenant_id erkennt
    exakte Duplikate (kein erneuter Import derselben Datei). Beleganker wird
    hier als Seiten-/Tabellentext aus services/pdf_parser gespeichert (volle
    strukturierte Extraktion mit Feld-Belegstellen folgt in Etappe 2)."""
    __tablename__ = "mdc_document_versions"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    document_id = Column(UUID(as_uuid=True), ForeignKey("mdc_documents.id", ondelete="CASCADE"), nullable=False, index=True)
    version_number = Column(Integer, nullable=False)

    file_name = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=False)
    file_hash = Column(String(64), nullable=False, index=True)
    file_size_bytes = Column(Integer, nullable=False)

    source = Column(String(255), nullable=True)
    rights_note = Column(Text, nullable=True)
    extracted_text = Column(Text, nullable=True)

    import_status = Column(SAEnum(MDCImportStatus, name="mdc_import_status"), default=MDCImportStatus.uploaded, nullable=False)
    import_error = Column(Text, nullable=True)
    superseded_by_version_id = Column(UUID(as_uuid=True), ForeignKey("mdc_document_versions.id", ondelete="SET NULL"), nullable=True)

    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class MDCLineItem(Base):
    """Eine Preisbeobachtung (price_observation + line_item der Anleitung,
    in einer Tabelle). original_amount/original_currency bleiben immer die
    unveraenderte Quellenangabe; Normalisierungen (Netto, Tag->Stunde)
    werden nur bei belegter Grundlage berechnet, sonst NULL."""
    __tablename__ = "mdc_line_items"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    document_version_id = Column(UUID(as_uuid=True), ForeignKey("mdc_document_versions.id", ondelete="CASCADE"), nullable=False, index=True)
    category_id = Column(UUID(as_uuid=True), ForeignKey("mdc_categories.id", ondelete="SET NULL"), nullable=True, index=True)
    supplier_id = Column(UUID(as_uuid=True), ForeignKey("mdc_suppliers.id", ondelete="SET NULL"), nullable=True, index=True)

    role_or_item = Column(String(255), nullable=True)
    scope_text = Column(Text, nullable=True)
    seniority = Column(String(100), nullable=True)
    region = Column(String(100), nullable=True)

    original_amount = Column(Numeric(18, 4), nullable=True)
    original_amount_raw = Column(String(100), nullable=True)  # exakt wie im Dokument, fuer Komma/Punkt-Pruefung
    amount_confirmed = Column(Boolean, default=False, nullable=False)  # Mensch hat Betrag gegen Original bestaetigt
    original_currency = Column(String(10), nullable=True)
    tax_basis = Column(SAEnum(MDCTaxBasis, name="mdc_tax_basis"), default=MDCTaxBasis.unknown, nullable=False)
    tax_rate_pct = Column(Numeric(5, 2), nullable=True)
    normalized_amount_net = Column(Numeric(18, 4), nullable=True)
    normalization_version = Column(String(50), nullable=True)

    original_unit = Column(String(50), nullable=True)
    canonical_unit = Column(String(50), nullable=True)
    quantity = Column(Numeric(18, 4), nullable=True)
    min_quantity = Column(Numeric(18, 4), nullable=True)
    billable_hours_per_day = Column(Numeric(6, 2), nullable=True)
    normalized_amount_per_canonical_unit = Column(Numeric(18, 4), nullable=True)

    ancillary_costs_json = Column(JSON, default=dict)
    payment_terms = Column(String(255), nullable=True)

    price_status = Column(SAEnum(MDCPriceStatus, name="mdc_price_status"), default=MDCPriceStatus.quoted, nullable=False)
    review_status = Column(SAEnum(MDCReviewStatus, name="mdc_review_status"), default=MDCReviewStatus.extracted, nullable=False)
    open_issues_json = Column(JSON, default=list)

    offer_date = Column(Date, nullable=True)
    valid_from = Column(Date, nullable=True)
    valid_to = Column(Date, nullable=True)

    source_evidence = Column(Text, nullable=True)
    reviewed_by = Column(String(100), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    review_note = Column(Text, nullable=True)
    superseded_by_line_item_id = Column(UUID(as_uuid=True), ForeignKey("mdc_line_items.id", ondelete="SET NULL"), nullable=True)

    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
