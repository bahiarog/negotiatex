"""
Teil B7-B9 (CTO-Playbook): Angebotsanfrage (RFQ), Angebotsvergleich/-verhandlung
und Vertragsentwurf bis Signatur-Bereitschaft.

Additive zu models_v2.py / models_negotiation.py / models_sourcing.py -- ruehrt
keine bestehende Tabelle an. Gleiche Konventionen: Geldbetraege als
Numeric/Decimal (nie Float), UUID-PKs, tenant_id ueberall, hash-gebundene
Freigabe fuer jeden ausgehenden Versand (identisches Muster wie
NegotiationAction/-Approval und OutreachAction/-Approval), append-only
Event-Tabelle (mirrors CaseEvent/NDAEvent).

Judgment call (siehe Bericht): B8 (Bewerten und Verhandeln) baut auf dem
Vergleich hier auf, verhandelt aber ueber die BESTEHENDEN Teil-A-Tabellen
(NegotiationStrategy/NegotiationAction/NegotiationApproval aus
models_negotiation.py) -- es gibt hier KEINE zweite Preisverhandlungs-Engine.
`/rfq/{id}/award` erzeugt lediglich einen neuen `Case` + eine neue
`NegotiationStrategy`, vorbefuellt aus dem Vergleich.

B9-Vertragsversionierung (Playbook-Zitat: "Eine neue Vertragsversion hebt die
bisherige Freigabe auf"): jede neue Version ist eine EIGENE `Contract`-Zeile
(gleiche `lineage_id`, `version` hochgezaehlt); die alte Zeile bekommt
`superseded_by_version` gesetzt. Jede Freigabe/Versand-Aktion (`ContractAction`)
ist an eine bestimmte `contract_id` (= eine bestimmte Version) gebunden -- eine
Aktion der alten Version kann nach dem Versions-Bump nicht mehr gesendet werden
(siehe routers/rfq_contracts.py `_ensure_contract_not_superseded`).
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


# ---------------------------------------------------------------------------
# B7 -- RFQ (Angebotsanfrage)
# ---------------------------------------------------------------------------

class RFQStatus(str, enum.Enum):
    draft = "draft"
    sent = "sent"                       # mind. eine Einladung versendet
    collecting_offers = "collecting_offers"
    comparison_ready = "comparison_ready"
    awarded = "awarded"                 # Zuschlag erteilt (-> Teil-A-Verhandlung laeuft)
    closed = "closed"


class RFQ(Base):
    """B7: 'Diese Anfrage begruendet keine Bestellung' -- spec_text ist der
    Spezifikationstext, gegen den Bieter ihre Abweichungen ausdruecklich
    kennzeichnen sollen. expected_quantity ist der erwartete Leistungsumfang
    (Menge), gegen den jedes eingehende Angebot bei der Vergleichbarkeits-
    pruefung (B8) abgeglichen wird -- weicht die Menge/der Umfang ab, darf das
    System daraus KEINE Einsparung bei identischem Umfang ableiten (siehe
    services/rfq_classifier.compute_offer_comparison)."""
    __tablename__ = "rfqs"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    sourcing_request_id = Column(UUID(as_uuid=True), ForeignKey("sourcing_requests.id", ondelete="CASCADE"), nullable=False, index=True)

    spec_version = Column(Integer, default=1, nullable=False)
    spec_text = Column(Text, nullable=False)
    expected_quantity = Column(Numeric(18, 4), nullable=True)
    expected_unit = Column(String(50), nullable=True)
    currency = Column(String(10), default="EUR", nullable=False)

    deadline = Column(DateTime, nullable=False)
    test_mode = Column(Boolean, default=True, nullable=False)  # steuert "[TEST – ]"-Betreffprefix

    status = Column(SAEnum(RFQStatus, name="rfq_status"), default=RFQStatus.draft, nullable=False)

    awarded_offer_id = Column(UUID(as_uuid=True), ForeignKey("rfq_offers.id", ondelete="SET NULL"), nullable=True)
    awarded_by = Column(String(100), nullable=True)
    awarded_at = Column(DateTime, nullable=True)
    awarded_case_id = Column(UUID(as_uuid=True), ForeignKey("cases.id", ondelete="SET NULL"), nullable=True)

    created_by = Column(String(100), nullable=False)
    created_at = Column(DateTime, server_default=func.now())


class RFQInvitation(Base):
    """Welche NDA-freigegebenen Kandidaten zu dieser RFQ eingeladen wurden.
    'sent_at' bleibt NULL, solange die Einladung nur entworfen (RFQAction),
    aber noch nicht tatsaechlich versendet wurde."""
    __tablename__ = "rfq_invitations"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    rfq_id = Column(UUID(as_uuid=True), ForeignKey("rfqs.id", ondelete="CASCADE"), nullable=False, index=True)
    supplier_candidate_id = Column(UUID(as_uuid=True), ForeignKey("supplier_candidates.id", ondelete="CASCADE"), nullable=False, index=True)

    sent_at = Column(DateTime, nullable=True)
    message_id = Column(String(998), nullable=True)

    created_at = Column(DateTime, server_default=func.now())
    __table_args__ = (UniqueConstraint("rfq_id", "supplier_candidate_id", name="uq_rfq_invitation_candidate"),)


class RFQActionStatus(str, enum.Enum):
    draft = "draft"
    approved = "approved"
    sent = "sent"
    rejected = "rejected"


class RFQAction(Base):
    """Jede ausgehende RFQ-E-Mail (Einladung, Rueckfragen-Antwort-Rundmail,
    Eingangsbestaetigung eines Angebots) -- exakt dasselbe Hash-Bindungsmuster
    wie models_sourcing.OutreachAction. 'kind' unterscheidet:
      invite                -- die eigentliche Angebotsanfrage an EINEN Bieter
      clarification_broadcast -- eine freigegebene Klarstellung, an ALLE
                                  betroffenen Bieter (je eine eigene Zeile/Mail,
                                  damit sealed-bid gewahrt bleibt)
      receipt_confirmation   -- Eingangsbestaetigung eines Angebots an den
                                  einreichenden Bieter
    Das Cross-Bidder-Vertraulichkeits-Guard (B7: 'individuelle Preise oder
    vertrauliche Konditionen eines Mitbewerbers werden nicht weitergegeben')
    wird vor dem Versand JEDER Zeile hier geprueft (siehe
    routers/rfq_contracts._check_cross_bidder_confidentiality)."""
    __tablename__ = "rfq_actions"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    rfq_id = Column(UUID(as_uuid=True), ForeignKey("rfqs.id", ondelete="CASCADE"), nullable=False, index=True)
    supplier_candidate_id = Column(UUID(as_uuid=True), ForeignKey("supplier_candidates.id", ondelete="CASCADE"), nullable=False, index=True)

    kind = Column(String(50), default="invite", nullable=False)

    recipient_email = Column(String(255), nullable=False)
    rendered_subject = Column(Text)
    rendered_body = Column(Text)
    payload_hash = Column(String(64), nullable=True)

    status = Column(SAEnum(RFQActionStatus, name="rfq_action_status"), default=RFQActionStatus.draft, nullable=False)

    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class RFQApproval(Base):
    __tablename__ = "rfq_approvals"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    action_id = Column(UUID(as_uuid=True), ForeignKey("rfq_actions.id", ondelete="CASCADE"), nullable=False, index=True)
    approved_by = Column(String(100), nullable=False)
    payload_hash = Column(String(64), nullable=False)
    approved_at = Column(DateTime, server_default=func.now())


class ComparabilityFlag(str, enum.Enum):
    comparable = "comparable"
    flagged_different_scope = "flagged_different_scope"


class OfferStatus(str, enum.Enum):
    submitted = "submitted"
    superseded = "superseded"   # durch eine neuere Version desselben Bieters (z.B. nach Verhandlung) abgeloest
    withdrawn = "withdrawn"


class RFQOffer(Base):
    """B7/B8: ein eingegangenes Angebot (manuell erfasst oder per Upload+KI-
    Extraktion, siehe services/rfq_classifier.py). Fehlende Felder bleiben
    NULL/'unknown' -- werden NIE geraten (identisches Prinzip wie
    models_v2.LineItem / suppliers.py EXTRACTION_SYSTEM_PROMPT).

    `version`/`superseded_by_id` bilden eine einfache Historie pro Bieter ab:
    ein nach Verhandlung (Teil A) aktualisiertes Angebot wird als NEUE Zeile
    mit version+1 gespeichert, die alte Zeile bekommt status=superseded und
    superseded_by_id gesetzt -- so bleibt 'Anbieter A' vs 'Anbieter A nach
    Verhandlung' im Vergleich (B8) nachvollziehbar, statt den urspruenglichen
    Preis zu ueberschreiben."""
    __tablename__ = "rfq_offers"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    rfq_id = Column(UUID(as_uuid=True), ForeignKey("rfqs.id", ondelete="CASCADE"), nullable=False, index=True)
    supplier_candidate_id = Column(UUID(as_uuid=True), ForeignKey("supplier_candidates.id", ondelete="CASCADE"), nullable=False, index=True)

    version = Column(Integer, default=1, nullable=False)
    superseded_by_id = Column(UUID(as_uuid=True), ForeignKey("rfq_offers.id", ondelete="SET NULL"), nullable=True)

    unit_price = Column(Numeric(18, 4), nullable=True)
    quantity = Column(Numeric(18, 4), nullable=True)
    freight_cost = Column(Numeric(18, 4), default=0)
    other_costs = Column(Numeric(18, 4), default=0)
    currency = Column(String(10), default="EUR")

    delivery_date = Column(String(100), nullable=True)
    payment_terms = Column(Text, nullable=True)
    offer_validity_until = Column(DateTime, nullable=True)

    scope_note = Column(Text, nullable=True)           # Bieter-Hinweis auf Abweichung vom Spezifikationstext
    spec_confirmed = Column(Boolean, nullable=True)     # "Spezifikation ausdruecklich bestaetigt?" -- NULL = unbekannt

    raw_extracted_json = Column(JSON, nullable=True)    # vollstaendige KI-Extraktion inkl. "unknown"-Feldern
    source_document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True)

    comparability_flag = Column(SAEnum(ComparabilityFlag, name="offer_comparability_flag"),
                                 default=ComparabilityFlag.comparable, nullable=False)
    comparability_note = Column(Text, nullable=True)

    status = Column(SAEnum(OfferStatus, name="offer_status"), default=OfferStatus.submitted, nullable=False)

    received_at = Column(DateTime, server_default=func.now())
    created_by = Column(String(100), nullable=True)


# ---------------------------------------------------------------------------
# B9 -- Vertraege
# ---------------------------------------------------------------------------

class ContractTemplate(Base):
    """Entwurf (B9): eine NEUE Vorlage (noch nicht von Legal freigegeben)
    darf nicht fuer einen Vertragsentwurf verwendet werden --
    `template_approved_by_legal` wird NIE automatisch auf True gesetzt,
    ausschliesslich ueber den /approve-by-legal Endpunkt."""
    __tablename__ = "contract_templates"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    version = Column(Integer, default=1, nullable=False)
    template_approved_by_legal = Column(Boolean, default=False, nullable=False)
    approved_by = Column(String(100), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    body_text = Column(Text, nullable=False)  # mit Platzhaltern wie {{price}}, {{delivery_date}}, {{payment_terms}}
    created_by = Column(String(100), nullable=False)
    created_at = Column(DateTime, server_default=func.now())


class ContractStatus(str, enum.Enum):
    draft = "draft"
    sent = "sent"
    returned = "returned"
    legal_review_required = "legal_review_required"
    commercial_review_required = "commercial_review_required"
    approved_by_buyer = "approved_by_buyer"           # B9 Abschluss: konsolidiert, aber NICHT rechtsverbindlich unterschrieben
    human_confirmed_signed = "human_confirmed_signed"  # NUR per explizitem Human-Endpunkt erreichbar


class Contract(Base):
    """B9. `lineage_id` bleibt ueber alle Versionen eines Vertrags gleich;
    `version` zaehlt hoch; `superseded_by_version` wird auf der ALTEN Zeile
    gesetzt, sobald eine neue Version angelegt wird ('Eine neue
    Vertragsversion hebt die bisherige Freigabe auf' -- technisch durchgesetzt
    in routers/rfq_contracts._ensure_contract_not_superseded, nicht nur
    dokumentiert).

    Kaufmaennisches Mandat (price/payment-terms/delivery-date) wird hier als
    expliziter, gespeicherter Rahmen gefuehrt -- identisches Konzept wie
    NegotiationStrategy.price_cap_net. Jede Abweichung ausserhalb des Mandats
    ODER jede Umfangsaenderung geht NIE automatisch durch, sondern routet auf
    einen Review-Status (siehe routers/rfq_contracts.return_contract)."""
    __tablename__ = "contracts"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    lineage_id = Column(UUID(as_uuid=True), nullable=False, index=True)

    sourcing_request_id = Column(UUID(as_uuid=True), ForeignKey("sourcing_requests.id", ondelete="SET NULL"), nullable=True)
    case_id = Column(UUID(as_uuid=True), ForeignKey("cases.id", ondelete="SET NULL"), nullable=True)
    rfq_id = Column(UUID(as_uuid=True), ForeignKey("rfqs.id", ondelete="SET NULL"), nullable=True)
    offer_id = Column(UUID(as_uuid=True), ForeignKey("rfq_offers.id", ondelete="SET NULL"), nullable=True)
    template_id = Column(UUID(as_uuid=True), ForeignKey("contract_templates.id", ondelete="SET NULL"), nullable=True)
    supplier_candidate_id = Column(UUID(as_uuid=True), ForeignKey("supplier_candidates.id", ondelete="SET NULL"), nullable=True)

    version = Column(Integer, default=1, nullable=False)
    superseded_by_version = Column(Integer, nullable=True)

    status = Column(SAEnum(ContractStatus, name="contract_status"), default=ContractStatus.draft, nullable=False)

    body_text = Column(Text, nullable=False)
    payload_hash = Column(String(64), nullable=True)

    # Kaufmaennisches Mandat (gespeicherter Rahmen, analog price_cap_net)
    mandate_price_cap = Column(Numeric(18, 4), nullable=True)
    mandate_payment_terms_days_max = Column(Integer, nullable=True)
    mandate_delivery_date_latest = Column(String(100), nullable=True)

    legal_review_required = Column(Boolean, default=False, nullable=False)
    commercial_review_required = Column(Boolean, default=False, nullable=False)
    redline_detected = Column(Boolean, nullable=True)
    redline_clause_categories = Column(JSON, default=list)  # z.B. ["liability","data_protection"]
    returned_text = Column(Text, nullable=True)
    open_points_json = Column(JSON, default=list)

    human_confirmed_signed = Column(Boolean, default=False, nullable=False)
    human_confirmed_signed_by = Column(String(100), nullable=True)
    human_confirmed_signed_at = Column(DateTime, nullable=True)

    created_by = Column(String(100), nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class ContractActionStatus(str, enum.Enum):
    draft = "draft"
    approved = "approved"
    sent = "sent"
    rejected = "rejected"


class ContractAction(Base):
    """Ausgehende Vertrags-Kommunikation -- identisches Hash-Bindungsmuster
    wie NegotiationAction/OutreachAction/RFQAction. 'kind':
      send_draft      -- den aktuellen Vertragsentwurf an den Bieter senden
      propose_clause  -- eine Ersatzklausel + Begleittext vorschlagen
                          (B9 Gegenvorschlag: Klausel-Text UND Nachricht
                          beide hinter dieser einen Freigabe)
    `contract_version_at_action` bindet die Aktion an die Vertragsversion, die
    zum Entwurfszeitpunkt aktuell war -- wird beim Versand erneut gegen
    contract.version/superseded_by_version geprueft."""
    __tablename__ = "contract_actions"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    contract_id = Column(UUID(as_uuid=True), ForeignKey("contracts.id", ondelete="CASCADE"), nullable=False, index=True)

    kind = Column(String(50), default="send_draft", nullable=False)
    clause_category = Column(String(50), nullable=True)
    proposed_clause_text = Column(Text, nullable=True)

    recipient_email = Column(String(255), nullable=False)
    rendered_subject = Column(Text)
    rendered_body = Column(Text)
    payload_hash = Column(String(64), nullable=True)

    status = Column(SAEnum(ContractActionStatus, name="contract_action_status"), default=ContractActionStatus.draft, nullable=False)
    contract_version_at_action = Column(Integer, nullable=False)

    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class ContractApproval(Base):
    __tablename__ = "contract_approvals"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    action_id = Column(UUID(as_uuid=True), ForeignKey("contract_actions.id", ondelete="CASCADE"), nullable=False, index=True)
    approved_by = Column(String(100), nullable=False)
    payload_hash = Column(String(64), nullable=False)
    approved_at = Column(DateTime, server_default=func.now())


class ContractEvent(Base):
    """Append-only Audit-Trail -- mirrors models_v2.CaseEvent / models_sourcing.NDAEvent 1:1."""
    __tablename__ = "contract_events"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contract_id = Column(UUID(as_uuid=True), ForeignKey("contracts.id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    from_status = Column(String(50), nullable=True)
    to_status = Column(String(50), nullable=False)
    actor = Column(String(100), nullable=False)
    reason = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
