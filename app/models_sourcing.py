"""
Teil B (CTO-Playbook B2-B6): Lieferanten-Sourcing, Erstkontakt, Stammblatt-
Aufnahme und NDA-Statusverfolgung.

Additive zu models_v2.py / models_negotiation.py -- ruehrt keine bestehende
Tabelle an. Gleiche Konventionen: Geldbetraege als Numeric/Decimal, UUID-PKs,
tenant_id ueberall, append-only Event-Tabelle fuer die NDA (mirrors CaseEvent).

Wichtiger Hinweis (siehe B6 im Playbook und Modul-Docstring von
routers/sourcing.py): Die NDA-Tabellen hier sind reine STATUS-VERFOLGUNG fuer
einen menschengefuehrten Prozess. Es wird KEINE rechtsverbindliche
elektronische Signatur implementiert oder behauptet -- weder hier noch in der
UI. "Freigegeben" bedeutet ausschliesslich: ein Mensch hat ueber den
/verify + /approve Endpunkt explizit bestaetigt, dass Version, Parteien,
Unterzeichner und Gegenzeichnung passen.
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
# B2 -- Suchauftrag
# ---------------------------------------------------------------------------

class SourcingRequestStatus(str, enum.Enum):
    draft = "draft"
    approved = "approved"
    active = "active"
    closed = "closed"


class SourcingRequest(Base):
    """Der 'Suchauftrag' (B2). Eine Kampagnenfreigabe (status=approved) ist
    KEIN Blankoscheck -- sie bindet Kategorie, Empfaengerkreis, Kontaktweg,
    zulaessige Inhalte, Hoechstanzahl und Laufzeit. Jede einzelne
    Outreach-Aktion wird in routers/sourcing.py gegen genau diese Felder
    geprueft, bevor sie erlaubt wird (siehe `_check_campaign_limits`)."""
    __tablename__ = "sourcing_requests"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    case_id = Column(UUID(as_uuid=True), ForeignKey("cases.id", ondelete="SET NULL"), nullable=True, index=True)

    title = Column(String(255), nullable=False)
    status = Column(SAEnum(SourcingRequestStatus, name="sourcing_request_status"),
                     default=SourcingRequestStatus.draft, nullable=False)

    # Bedarf und Muss-Kriterien
    bedarf_text = Column(Text, nullable=False)
    must_criteria_json = Column(JSON, nullable=False, default=dict)  # z.B. {"masse": "...", "material": "...", "aenderung_untersagt": true}

    # Gebiet und Lieferfaehigkeit
    region = Column(String(255))
    delivery_location = Column(String(255))
    delivery_capability_confirmed = Column(Boolean, default=False)  # muss bestaetigt, nie angenommen werden

    # Budget und Vergleich (inkl. Fracht)
    budget_target = Column(Numeric(18, 2))
    budget_ceiling = Column(Numeric(18, 2), nullable=False)  # harte Obergrenze
    includes_freight = Column(Boolean, default=True)
    currency = Column(String(10), default="EUR")

    # Bewertung / Gewichtung (Default 50/30/20, vom Einkauf bestaetigt/anpassbar)
    weight_price = Column(Integer, default=50, nullable=False)
    weight_quality = Column(Integer, default=30, nullable=False)
    weight_delivery = Column(Integer, default=20, nullable=False)

    # Sourcing-Limit
    max_candidates_total = Column(Integer, default=10, nullable=False)
    max_contacted = Column(Integer, default=5, nullable=False)

    # Vertraulichkeit
    public_teaser_text = Column(Text)             # vor NDA zulaessig
    confidential_notice = Column(Text, default="Interne Zeichnungen/Spezifikationen erst nach bestaetigter NDA-Freigabe.")

    # Kommunikation
    sender_account = Column(String(255), default="info@negotiatex.ai")
    allowed_channels_json = Column(JSON, default=lambda: ["email"])
    message_template_key = Column(String(100), default="erstkontakt_v1")
    response_deadline_days = Column(Integer, default=5)
    reminder_schedule_minutes_json = Column(JSON, default=lambda: [15, 40])  # testbar kurz; real ~2/5 Werktage

    # Verantwortliche (nur erfasst, keine echte RBAC fuer dieses MVP)
    responsible_procurement = Column(String(255))  # Bedarf/Vergabeentscheidung
    responsible_legal = Column(String(255))        # rechtliche Klauseln
    responsible_finance = Column(String(255))      # Budget

    created_by = Column(String(100), nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    approved_by = Column(String(100), nullable=True)
    approved_at = Column(DateTime, nullable=True)


# ---------------------------------------------------------------------------
# B3 -- Lieferanten finden (Kandidaten, KEINE Live-Suche/Scraping -- siehe
# routers/sourcing.py Moduldoc)
# ---------------------------------------------------------------------------

class CandidateStatus(str, enum.Enum):
    """Bewusst vier (und mehr) GENAU UNTERSCHIEDENE Stati -- das System darf
    'gefunden', 'kontaktiert', 'antwortet' und 'qualifiziert' laut Playbook
    niemals vermischen."""
    found = "found"                        # gefunden
    shortlisted = "shortlisted"             # Shortlist nach Pruefung Muss-Kriterien
    contacted = "contacted"                 # kontaktiert (Erstkontakt gesendet)
    responded = "responded"                 # antwortet (Antwort eingegangen, noch nicht klassifiziert ausgewertet)
    interested = "interested"               # Interesse bestaetigt
    declined = "declined"                   # Absage
    onboarding_pending = "onboarding_pending"  # Stammblatt wird erhoben
    nda_review = "nda_review"               # NDA im Prozess (Entwurf/Zustellung/Ruecklauf/Pruefung)
    nda_approved = "nda_approved"           # NDA freigegeben -> Zugang zu vertraulichen Unterlagen moeglich
    qualified = "qualified"                 # qualifiziert (Stammblatt vollstaendig + NDA freigegeben)
    rejected = "rejected"                   # aus anderem Grund ausgeschieden


class SupplierCandidate(Base):
    """B3: jeder Kandidat hat eine nachvollziehbare Quelle (source_url +
    retrieved_at) -- auch wenn ein menschlicher Operator die URL manuell
    eingetragen hat. Es gibt KEINE automatische Websuche/Scraper-Integration
    in diesem MVP (siehe Moduldoc routers/sourcing.py)."""
    __tablename__ = "supplier_candidates"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    sourcing_request_id = Column(UUID(as_uuid=True), ForeignKey("sourcing_requests.id", ondelete="CASCADE"), nullable=False, index=True)

    company_name = Column(String(255), nullable=False)
    domain = Column(String(255))  # fuer einfachen Duplikat-Abgleich (Name+Domain, case-insensitive)
    location = Column(String(255))
    service_match_note = Column(Text)   # welches Bedarf/Leistung dieser Kandidat angeblich abdeckt
    public_address = Column(Text)
    contact_email = Column(String(255))
    contact_name = Column(String(255))

    source_url = Column(String(1000))             # manuell eingetragene Quelle -- NIE automatisch generiert
    retrieved_at = Column(DateTime)                # wann die Quelle gesichtet wurde

    existing_supplier_id = Column(UUID(as_uuid=True), ForeignKey("suppliers_v2.id", ondelete="SET NULL"), nullable=True)
    duplicate_of_id = Column(UUID(as_uuid=True), ForeignKey("supplier_candidates.id", ondelete="SET NULL"), nullable=True)

    must_criteria_check_json = Column(JSON, default=dict)  # {criterion: "met"|"unmet"|"unknown"}, NIE geraten
    open_questions_json = Column(JSON, default=list)        # aus "unknown"-Feldern abgeleitete Fragen an den Kandidaten

    status = Column(SAEnum(CandidateStatus, name="candidate_status"), default=CandidateStatus.found, nullable=False)

    stammblatt_json = Column(JSON, default=dict)  # per-Feld {status: fehlt|eingegangen|geprueft|freigegeben, source, reviewer, updated_at}

    # KI-Einschaetzung, ob vor der vollen Briefing-Weitergabe ein NDA noetig
    # ist (nutzerseitig gefordert: "muss der Agent abwaegen") -- Vorschlag,
    # kein autonomer Beschluss: {needs_nda, reasoning, assessed_at}. Ein NDA
    # wird dadurch nie automatisch versendet, nur vorbereitet/markiert.
    nda_assessment_json = Column(JSON, nullable=True)

    created_by = Column(String(100), nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class CandidateOnboardingInviteStatus(str, enum.Enum):
    pending = "pending"
    completed = "completed"


class CandidateOnboardingInvite(Base):
    """Selbstauskunfts-Link fuer einen interessierten Kandidaten, analog zum
    bestehenden (nicht mandantenfaehigen) SupplierInvite-Muster in
    routers/suppliers.py, aber an SupplierCandidate + tenant_id gebunden.
    Token wird nur gehasht gespeichert; einmal verwendet -> completed."""
    __tablename__ = "candidate_onboarding_invites"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    supplier_candidate_id = Column(UUID(as_uuid=True), ForeignKey("supplier_candidates.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True)
    status = Column(SAEnum(CandidateOnboardingInviteStatus, name="candidate_onboarding_invite_status"),
                     default=CandidateOnboardingInviteStatus.pending, nullable=False)
    confirmed_by_name = Column(String(255), nullable=True)
    confirmed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())


class SupplierCertificate(Base):
    """B5 Nachweise: 'ein angehoengtes PDF allein beweist keine Eignung' --
    verified bleibt False, bis ein Mensch explizit den /verify-Endpunkt
    aufruft. Kein Upload-Handler setzt verified je automatisch auf True."""
    __tablename__ = "supplier_certificates"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    supplier_candidate_id = Column(UUID(as_uuid=True), ForeignKey("supplier_candidates.id", ondelete="CASCADE"), nullable=True, index=True)
    supplier_v2_id = Column(UUID(as_uuid=True), ForeignKey("suppliers_v2.id", ondelete="CASCADE"), nullable=True, index=True)

    name = Column(String(255), nullable=False)
    issuer = Column(String(255))
    valid_until = Column(DateTime, nullable=True)
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True)

    verified = Column(Boolean, default=False, nullable=False)
    verified_by = Column(String(100), nullable=True)
    verified_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, server_default=func.now())


# ---------------------------------------------------------------------------
# B4 -- Erstkontakt (mirrors models_negotiation.EmailMessage, aber an
# supplier_candidate_id statt case_id gebunden)
# ---------------------------------------------------------------------------

class OutreachDirection(str, enum.Enum):
    outbound = "outbound"
    inbound = "inbound"


class OutreachActionStatus(str, enum.Enum):
    draft = "draft"
    pending_approval = "pending_approval"
    approved = "approved"
    sent = "sent"
    rejected = "rejected"


class OutreachAction(Base):
    """Entwurfs-/Freigabeobjekt fuer eine einzelne Erstkontakt- oder
    Erinnerungs-Mail. Exakt dasselbe Hash-Bindungsmuster wie
    models_negotiation.NegotiationAction/-Approval (siehe
    routers/sourcing.py `_compute_outreach_hash`)."""
    __tablename__ = "outreach_actions"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    supplier_candidate_id = Column(UUID(as_uuid=True), ForeignKey("supplier_candidates.id", ondelete="CASCADE"), nullable=False, index=True)

    kind = Column(String(50), default="first_contact")  # first_contact | reminder | stammblatt_request
    reminder_number = Column(Integer, default=0)  # 0 = Erstkontakt, 1/2 = Reminder

    recipient_email = Column(String(255), nullable=False)
    rendered_subject = Column(Text)
    rendered_body = Column(Text)
    payload_hash = Column(String(64), nullable=True)

    status = Column(SAEnum(OutreachActionStatus, name="outreach_action_status"),
                     default=OutreachActionStatus.draft, nullable=False)

    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class OutreachApproval(Base):
    __tablename__ = "outreach_approvals"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    action_id = Column(UUID(as_uuid=True), ForeignKey("outreach_actions.id", ondelete="CASCADE"), nullable=False, index=True)
    approved_by = Column(String(100), nullable=False)
    payload_hash = Column(String(64), nullable=False)
    approved_at = Column(DateTime, server_default=func.now())


class OutreachMessage(Base):
    """Mirrors models_negotiation.EmailMessage 1:1, aber an
    supplier_candidate_id gebunden statt case_id. message_id bleibt
    tenant-global UNIQUE (gleiche Idempotenz-Logik wie Teil A)."""
    __tablename__ = "outreach_messages"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    supplier_candidate_id = Column(UUID(as_uuid=True), ForeignKey("supplier_candidates.id", ondelete="CASCADE"), nullable=False, index=True)

    direction = Column(SAEnum(OutreachDirection, name="outreach_direction"), nullable=False)
    message_id = Column(String(998), nullable=False)
    in_reply_to = Column(String(998), nullable=True)
    references_header = Column(Text, nullable=True)

    from_addr = Column(String(255), nullable=False)
    to_addr = Column(String(255), nullable=False)
    subject = Column(Text)
    body_text = Column(Text)
    raw_source = Column(Text)

    occurred_at = Column(DateTime, server_default=func.now())

    __table_args__ = (UniqueConstraint("message_id", name="uq_outreach_message_id"),)


class OutreachReminderTimer(Base):
    """Haelt den naechsten faelligen Reminder-Zeitpunkt pro Kandidat. Jede
    eingehende Antwort (gleich welcher Klassifikation) oder ein manueller
    Abbruch setzt `cancelled=True` -- 'jede Antwort oder jeder Einwand
    storniert alle ausstehenden Erinnerungs-Timer fuer diesen Kandidaten
    sofort' (B4)."""
    __tablename__ = "outreach_reminder_timers"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    supplier_candidate_id = Column(UUID(as_uuid=True), ForeignKey("supplier_candidates.id", ondelete="CASCADE"), nullable=False, index=True)
    reminder_number = Column(Integer, nullable=False)  # 1 oder 2
    due_at = Column(DateTime, nullable=False)
    fired = Column(Boolean, default=False, nullable=False)
    cancelled = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, server_default=func.now())


# ---------------------------------------------------------------------------
# B6 -- NDA Workflow (Status-Tracking, KEINE rechtsverbindliche E-Signatur)
# ---------------------------------------------------------------------------

class NDAStatus(str, enum.Enum):
    draft = "draft"                  # Entwurf
    sent = "sent"                    # Zustellung
    returned = "returned"            # Ruecklauf eingegangen
    review_required = "review_required"  # Redline/Abweichung erkannt -> Legal
    verified = "verified"            # Mensch hat Version/Parteien/Signatar/Gegenzeichnung geprueft
    approved = "approved"            # NUR per explizitem Human-Endpoint erreichbar
    rejected = "rejected"


class NDA(Base):
    """Durably stored (Playbook-Zitat): NDA-Version und Hash, Versand,
    Signaturereignisse, Parteien, Pruefung der Unterzeichnungsbefugnis,
    Freigabe und zugaengliche Dokumente. Ein Signaturbild oder die blosse
    Aussage 'ist unterschrieben' im Rueckmeldetext setzt status NIEMALS auf
    approved -- das kann ausschliesslich der /verify + /approve Endpunkt in
    routers/sourcing.py (menschlich ausgeloest)."""
    __tablename__ = "ndas"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    supplier_candidate_id = Column(UUID(as_uuid=True), ForeignKey("supplier_candidates.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)

    template_version = Column(String(50), default="standard_v1")
    party_a_name = Column(String(255), default="Auftraggeber (vertreten durch NegotiateX.ai)")
    party_b_name = Column(String(255))  # Firmenname des Kandidaten (aus Stammblatt)
    signatory_name = Column(String(255))
    signatory_authorized_confirmed = Column(Boolean, default=False)  # vom Menschen bestaetigt

    draft_text = Column(Text)
    draft_hash = Column(String(64))

    status = Column(SAEnum(NDAStatus, name="nda_status"), default=NDAStatus.draft, nullable=False)

    sent_at = Column(DateTime, nullable=True)
    sent_message_id = Column(String(998), nullable=True)
    sent_payload_hash = Column(String(64), nullable=True)  # Freigabe-Bindung, analog Negotiation

    returned_at = Column(DateTime, nullable=True)
    returned_text = Column(Text, nullable=True)
    returned_hash = Column(String(64), nullable=True)
    redline_detected = Column(Boolean, nullable=True)  # None = noch kein Ruecklauf
    apparent_signature_claim = Column(Boolean, default=False)  # Text ENTHAELT "unterschrieben"/"signed" -- reines Signal, NIE Statusgrundlage

    verified = Column(Boolean, default=False, nullable=False)
    verified_by = Column(String(100), nullable=True)
    verified_at = Column(DateTime, nullable=True)
    verification_checklist_json = Column(JSON, default=dict)  # {version_matches, parties_match, signatory_authorized, countersignature_present}

    approved_by = Column(String(100), nullable=True)
    approved_at = Column(DateTime, nullable=True)

    created_by = Column(String(100), nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class NDAEvent(Base):
    """Append-only Audit-Trail -- mirrors models_v2.CaseEvent 1:1."""
    __tablename__ = "nda_events"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    nda_id = Column(UUID(as_uuid=True), ForeignKey("ndas.id", ondelete="CASCADE"), nullable=False, index=True)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    from_status = Column(String(50), nullable=True)
    to_status = Column(String(50), nullable=False)
    actor = Column(String(100), nullable=False)  # user id (str) oder "system"
    reason = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
