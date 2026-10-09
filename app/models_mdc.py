"""
Master Data Center (Etappe 1 -- Datenkern): Kategorie/Spezifikation,
Lieferanten-Identitaet, Originaldokumente und versionierte Dokumentfassungen
mit Beleganker (Seiten-/Tabellentext aus services/pdf_parser).

Bewusst NICHT Teil von Etappe 1 (folgt in Etappe 2/3, siehe Anleitung
"Master Data Center, Preisanalytik und RAG", Abschnitt 16):
  - price_observation/line_item (strukturierte Preispositionen)
  - field_evidence/review (Review-Queue mit Feldfreigabe)
  - retrieval_chunk (pgvector-Embeddings)
  - analysis_snapshot (Vergleichsergebnisse)
Etappe 1 liefert den Unterbau dafuer: Originale sicher gespeichert, Hash-
basierte Duplikaterkennung pro Mandant, Versionierung statt stillem
Ueberschreiben, Rechte-/Quellenvermerk je Version.
"""
import enum
import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text, func
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
