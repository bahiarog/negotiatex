"""
Teil B (CTO-Playbook B2-B6): Suchauftrag, Lieferantensuche (Kandidaten,
Erstkontakt), Stammblatt-Aufnahme und NDA-Statusverfolgung.

Explizit NICHT gebaut (siehe Bericht fuer Begruendung):
  - KEINE Live-Websuche / Scraping / Search-API-Integration (B3). Kandidaten
    werden von einem menschlichen Operator erfasst; das Datenmodell ist aber
    so aufgebaut, als kaeme jeder Kandidat aus einer nachvollziehbaren Quelle
    (source_url + retrieved_at), damit spaeter eine echte Recherchequelle
    ohne Schemaaenderung angebunden werden koennte.
  - KEINE rechtsverbindliche elektronische Signatur (B6). Die NDA-Tabellen
    sind reine Status-Verfolgung eines menschengefuehrten Prozesses. Kein
    Code-Pfad kann NDA.status je automatisch auf 'approved' setzen -- nur der
    explizite, menschlich ausgeloeste /approve-Endpunkt (siehe
    `approve_nda` unten), und der verlangt zuvor einen menschlichen
    /verify-Aufruf (`verify_nda`).

Gleiche Sicherheitsphilosophie wie routers/negotiation.py und routers/chat.py:
jeder Anthropic-Call (in services/sourcing_classifier.py) bekommt KEIN
`tools`-Argument und loest nie direkt eine sendende/freigebende Aktion aus.
Versand (Outreach, NDA) folgt demselben hash-gebundenen Freigabemuster wie
Teil A: der bei Freigabe gespeicherte Hash wird unmittelbar vor dem
tatsaechlichen Versand aus dem aktuellen Inhalt neu berechnet und verglichen.
"""
import hashlib
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from email.utils import make_msgid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, desc, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db, get_admin_db
from deps import get_current_user, get_current_membership
from models_v2 import SupplierV2
from models_sourcing import (
    SourcingRequest, SourcingRequestStatus,
    SupplierCandidate, CandidateStatus,
    SupplierCertificate,
    OutreachAction, OutreachApproval, OutreachActionStatus,
    OutreachMessage, OutreachDirection, OutreachReminderTimer,
    NDA, NDAEvent, NDAStatus,
    CandidateOnboardingInvite, CandidateOnboardingInviteStatus,
)
from models_contracts import RFQ, RFQInvitation, RFQAction, RFQActionStatus
from services.email_sender import send_negotiation_email
from services.sourcing_classifier import classify_outreach_reply, detect_redline, detect_signature_claim, assess_nda_necessity

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class SourcingRequestCreate(BaseModel):
    title: str
    case_id: Optional[str] = None
    bedarf_text: str
    must_criteria_json: dict = {}
    region: Optional[str] = None
    delivery_location: Optional[str] = None
    delivery_capability_confirmed: bool = False
    budget_target: Optional[Decimal] = None
    budget_ceiling: Decimal
    includes_freight: bool = True
    currency: str = "EUR"
    weight_price: int = 50
    weight_quality: int = 30
    weight_delivery: int = 20
    max_candidates_total: int = 10
    max_contacted: int = 5
    public_teaser_text: Optional[str] = None
    sender_account: str = "info@negotiatex.ai"
    allowed_channels_json: list[str] = ["email"]
    response_deadline_days: int = 5
    reminder_schedule_minutes_json: list[int] = [15, 40]
    responsible_procurement: Optional[str] = None
    responsible_legal: Optional[str] = None
    responsible_finance: Optional[str] = None


class CandidateCreate(BaseModel):
    company_name: str
    domain: Optional[str] = None
    location: Optional[str] = None
    service_match_note: Optional[str] = None
    public_address: Optional[str] = None
    contact_email: Optional[str] = None
    contact_name: Optional[str] = None
    source_url: str
    retrieved_at: Optional[datetime] = None


class MustCriteriaUpdate(BaseModel):
    checks: dict  # {criterion_key: "met"|"unmet"|"unknown"}


class BankDataUpdate(BaseModel):
    iban: Optional[str] = None
    bic: Optional[str] = None
    bank_name: Optional[str] = None


class VerifyPayload(BaseModel):
    verified_by: Optional[str] = None


class StammblattFieldUpdate(BaseModel):
    field_name: str
    value: Optional[str] = None
    status: str  # fehlt | eingegangen | geprueft | freigegeben
    source: Optional[str] = None
    reviewer: Optional[str] = None


class CertificateCreate(BaseModel):
    name: str
    issuer: Optional[str] = None
    valid_until: Optional[datetime] = None
    document_id: Optional[str] = None


class TestInboundOutreachReply(BaseModel):
    """NUR fuer Tests/Verifikation -- analog zu negotiation.py's
    test-inbound-Endpunkt. Simuliert exakt das, was der IMAP-Poller aus
    einer echten Antwort erzeugen wuerde."""
    from_addr: str
    subject: str
    body_text: str


class NDAReturnPayload(BaseModel):
    """Simuliert/erfasst den Ruecklauf-Text (entweder manuell eingefuegt oder
    vom IMAP-Poller als Body einer Antwort-Mail geliefert)."""
    returned_text: str


class NDAVerifyPayload(BaseModel):
    version_matches: bool
    parties_match: bool
    signatory_authorized: bool
    countersignature_present: bool
    verified_by: Optional[str] = None
    note: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_request_or_404(request_id: str, tenant_id, db: AsyncSession) -> SourcingRequest:
    try:
        rid = uuid.UUID(request_id)
    except ValueError:
        raise HTTPException(404, "Suchauftrag nicht gefunden.")
    r = await db.execute(select(SourcingRequest).where(SourcingRequest.id == rid))
    req = r.scalar_one_or_none()
    if not req or req.tenant_id != tenant_id:
        raise HTTPException(404, "Suchauftrag nicht gefunden.")
    return req


async def _get_candidate_or_404(candidate_id: str, tenant_id, db: AsyncSession) -> SupplierCandidate:
    try:
        cid = uuid.UUID(candidate_id)
    except ValueError:
        raise HTTPException(404, "Kandidat nicht gefunden.")
    r = await db.execute(select(SupplierCandidate).where(SupplierCandidate.id == cid))
    cand = r.scalar_one_or_none()
    if not cand or cand.tenant_id != tenant_id:
        raise HTTPException(404, "Kandidat nicht gefunden.")
    return cand


async def _get_action_or_404(action_id: str, candidate_id, db: AsyncSession) -> OutreachAction:
    try:
        aid = uuid.UUID(action_id)
    except ValueError:
        raise HTTPException(404, "Aktion nicht gefunden.")
    r = await db.execute(select(OutreachAction).where(OutreachAction.id == aid))
    action = r.scalar_one_or_none()
    if not action or action.supplier_candidate_id != candidate_id:
        raise HTTPException(404, "Aktion nicht gefunden.")
    return action


async def _get_nda_or_404(nda_id: str, tenant_id, db: AsyncSession) -> NDA:
    try:
        nid = uuid.UUID(nda_id)
    except ValueError:
        raise HTTPException(404, "NDA nicht gefunden.")
    r = await db.execute(select(NDA).where(NDA.id == nid))
    nda = r.scalar_one_or_none()
    if not nda or nda.tenant_id != tenant_id:
        raise HTTPException(404, "NDA nicht gefunden.")
    return nda


async def _get_supplier_or_404(supplier_id: str, tenant_id, db: AsyncSession) -> SupplierV2:
    try:
        sid = uuid.UUID(supplier_id)
    except ValueError:
        raise HTTPException(404, "Lieferant nicht gefunden.")
    r = await db.execute(select(SupplierV2).where(SupplierV2.id == sid))
    sup = r.scalar_one_or_none()
    if not sup or sup.tenant_id != tenant_id:
        raise HTTPException(404, "Lieferant nicht gefunden.")
    return sup


async def _nda_transition(db: AsyncSession, nda: NDA, new_status: NDAStatus, actor: str, reason: str):
    old = nda.status
    old_value = old.value if hasattr(old, "value") else old
    nda.status = new_status
    nda.updated_at = datetime.utcnow()
    db.add(NDAEvent(
        nda_id=nda.id, tenant_id=nda.tenant_id,
        from_status=old_value, to_status=new_status.value,
        actor=actor, reason=reason,
    ))


def _compute_hash(*parts) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _check_campaign_limits(req: SourcingRequest, db: AsyncSession, about_to_contact: bool = False) -> Optional[str]:
    """B2-Kernprinzip: eine Kampagnenfreigabe bindet Hoechstanzahl und
    Empfaengerkreis. Jede einzelne Outreach-Aktion wird hier GEGEN die in
    der SourcingRequest gespeicherten Limits geprueft -- Ueberschreitung wird
    NIE automatisch erlaubt, sondern blockiert (der Operator muss den
    Suchauftrag anpassen oder einen anderen Kandidaten waehlen)."""
    if req.status != SourcingRequestStatus.approved and req.status != SourcingRequestStatus.active:
        return "Suchauftrag ist noch nicht freigegeben (status != approved/active) -- keine Kontaktaufnahme moeglich."

    total = (await db.execute(
        select(sa_func.count()).select_from(SupplierCandidate).where(
            SupplierCandidate.sourcing_request_id == req.id,
            SupplierCandidate.status != CandidateStatus.rejected,
        )
    )).scalar_one()
    if total > req.max_candidates_total:
        return f"Sourcing-Limit ueberschritten: {total} Kandidaten > max_candidates_total={req.max_candidates_total}."

    if about_to_contact:
        contacted_states = (
            CandidateStatus.contacted, CandidateStatus.responded, CandidateStatus.interested,
            CandidateStatus.onboarding_pending, CandidateStatus.nda_review,
            CandidateStatus.nda_approved, CandidateStatus.qualified,
        )
        contacted = (await db.execute(
            select(sa_func.count()).select_from(SupplierCandidate).where(
                SupplierCandidate.sourcing_request_id == req.id,
                SupplierCandidate.status.in_(contacted_states),
            )
        )).scalar_one()
        if contacted >= req.max_contacted:
            return (f"Kontakt-Limit erreicht: bereits {contacted} von max_contacted={req.max_contacted} "
                     "Kandidaten kontaktiert. Neuer Kontakt ausserhalb dieser Regel wird zur Pruefung "
                     "vorgelegt -- bitte Suchauftrag anpassen oder anderen Kandidaten waehlen.")
    return None


FIRST_CONTACT_TEMPLATE_SUBJECT = "[TEST – ]Anfrage zu {bedarf} / {vorgangs_id}"
FIRST_CONTACT_TEMPLATE_BODY = (
    "Guten Tag,\n\nwir koordinieren fuer den Auftraggeber eine Beschaffungsanfrage ueber {bedarf}. "
    "Koennen Sie grundsaetzlich die angefragte Leistung/Produktart liefern? Bitte teilen Sie uns mit, "
    "ob Sie Interesse an einer Angebotsabgabe haben und wer dafuer Ihr zustaendiger Kontakt ist. Nach "
    "Ihrer Rueckmeldung stellen wir Ihnen die freigegebenen Unterlagen bereit.\n\n"
    "Diese Anfrage ist keine Bestellung.\n\nFreundliche Gruesse,\nNegotiateX – KI-gestuetzte "
    "Beschaffungskoordination.\nTestlauf."
)

STAMMBLATT_TEMPLATE_SUBJECT = "[TEST – ]Stammdaten und NDA zu {bedarf} / {vorgangs_id}"
STAMMBLATT_TEMPLATE_BODY = (
    "Guten Tag,\n\nvielen Dank fuer Ihr Interesse. Um den Vorgang fortzusetzen, bitten wir Sie, unser "
    "Lieferanten-Stammblatt zu vervollstaendigen (Firmendaten, Ansprechpartner fuer Angebot und Vertrag, "
    "Leistungsangaben und ggf. erforderliche Nachweise) und den beigefuegten NDA-Entwurf zu pruefen bzw. "
    "zu unterzeichnen. Bitte teilen Sie uns zudem mit, wer auf Ihrer Seite zur Unterzeichnung "
    "berechtigt ist.\n\nFreundliche Gruesse,\nNegotiateX – KI-gestuetzte Beschaffungskoordination.\n"
    "Testlauf."
)


# ---------------------------------------------------------------------------
# B2 -- SourcingRequest CRUD
# ---------------------------------------------------------------------------

@router.post("/requests")
async def create_sourcing_request(
    payload: SourcingRequestCreate,
    user=Depends(get_current_user), membership=Depends(get_current_membership),
    db: AsyncSession = Depends(get_db),
):
    if payload.weight_price + payload.weight_quality + payload.weight_delivery != 100:
        raise HTTPException(400, "Bewertungsgewichte (Preis/Qualitaet/Lieferfaehigkeit) muessen in Summe 100 ergeben.")
    case_uuid = None
    if payload.case_id:
        try:
            case_uuid = uuid.UUID(payload.case_id)
        except ValueError:
            raise HTTPException(400, "Ungueltige case_id.")

    req = SourcingRequest(
        tenant_id=membership.tenant_id, case_id=case_uuid, title=payload.title,
        status=SourcingRequestStatus.draft, bedarf_text=payload.bedarf_text,
        must_criteria_json=payload.must_criteria_json, region=payload.region,
        delivery_location=payload.delivery_location,
        delivery_capability_confirmed=payload.delivery_capability_confirmed,
        budget_target=payload.budget_target, budget_ceiling=payload.budget_ceiling,
        includes_freight=payload.includes_freight, currency=payload.currency,
        weight_price=payload.weight_price, weight_quality=payload.weight_quality,
        weight_delivery=payload.weight_delivery,
        max_candidates_total=payload.max_candidates_total, max_contacted=payload.max_contacted,
        public_teaser_text=payload.public_teaser_text, sender_account=payload.sender_account,
        allowed_channels_json=payload.allowed_channels_json,
        response_deadline_days=payload.response_deadline_days,
        reminder_schedule_minutes_json=payload.reminder_schedule_minutes_json,
        responsible_procurement=payload.responsible_procurement,
        responsible_legal=payload.responsible_legal, responsible_finance=payload.responsible_finance,
        created_by=str(user.id),
    )
    db.add(req)
    await db.commit()
    await db.refresh(req)
    return _request_to_dict(req)


def _request_to_dict(r: SourcingRequest) -> dict:
    return {
        "id": str(r.id), "title": r.title, "status": r.status.value if hasattr(r.status, "value") else r.status,
        "bedarf_text": r.bedarf_text, "must_criteria_json": r.must_criteria_json,
        "region": r.region, "delivery_location": r.delivery_location,
        "delivery_capability_confirmed": r.delivery_capability_confirmed,
        "budget_target": str(r.budget_target) if r.budget_target is not None else None,
        "budget_ceiling": str(r.budget_ceiling), "includes_freight": r.includes_freight,
        "currency": r.currency,
        "weights": {"price": r.weight_price, "quality": r.weight_quality, "delivery": r.weight_delivery},
        "max_candidates_total": r.max_candidates_total, "max_contacted": r.max_contacted,
        "responsible": {
            "procurement": r.responsible_procurement, "legal": r.responsible_legal,
            "finance": r.responsible_finance,
        },
        "created_at": r.created_at, "approved_by": r.approved_by, "approved_at": r.approved_at,
    }


@router.get("/requests")
async def list_sourcing_requests(membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(SourcingRequest).where(SourcingRequest.tenant_id == membership.tenant_id).order_by(desc(SourcingRequest.created_at)))
    return [_request_to_dict(x) for x in r.scalars().all()]


@router.get("/requests/{request_id}")
async def get_sourcing_request(request_id: str, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    req = await _get_request_or_404(request_id, membership.tenant_id, db)
    return _request_to_dict(req)


@router.post("/requests/{request_id}/approve")
async def approve_sourcing_request(
    request_id: str, user=Depends(get_current_user),
    membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db),
):
    """B2-Kernzitat: 'Eine Kampagnenfreigabe enthaelt nicht nur ein Ja.' --
    diese Freigabe setzt lediglich status=approved; sie ersetzt NICHT die
    Pro-Kandidat-Limit-Pruefung in `_check_campaign_limits`, die bei jeder
    einzelnen Outreach-Aktion erneut laeuft."""
    req = await _get_request_or_404(request_id, membership.tenant_id, db)
    if req.status != SourcingRequestStatus.draft:
        raise HTTPException(400, f"Suchauftrag im Status '{req.status.value}' kann nicht (erneut) freigegeben werden.")
    if not req.delivery_capability_confirmed:
        raise HTTPException(400, "Lieferfaehigkeit/Gebiet muss vor Freigabe bestaetigt werden (delivery_capability_confirmed).")
    req.status = SourcingRequestStatus.approved
    req.approved_by = str(user.id)
    req.approved_at = datetime.utcnow()
    await db.commit()
    return _request_to_dict(req)


# ---------------------------------------------------------------------------
# B3 -- Kandidaten
# ---------------------------------------------------------------------------

def _norm_name_domain(name: str, domain: Optional[str]) -> tuple[str, str]:
    return (name or "").strip().lower(), (domain or "").strip().lower()


@router.post("/requests/{request_id}/candidates")
async def add_candidate(
    request_id: str, payload: CandidateCreate,
    user=Depends(get_current_user), membership=Depends(get_current_membership),
    db: AsyncSession = Depends(get_db),
):
    req = await _get_request_or_404(request_id, membership.tenant_id, db)

    limit_problem = await _check_campaign_limits(req, db, about_to_contact=False)
    # Beim blossen Erfassen eines Kandidaten gilt nur das Gesamtlimit, noch
    # nicht das Kontakt-Limit -- Erfassen != Kontaktieren (B3 Punkt 5: jeder
    # Kandidat braucht menschliche Pruefung VOR Kontaktaufnahme, Erfassen
    # selbst ist noch kein Kontakt).
    total = (await db.execute(
        select(sa_func.count()).select_from(SupplierCandidate).where(
            SupplierCandidate.sourcing_request_id == req.id,
            SupplierCandidate.status != CandidateStatus.rejected,
        )
    )).scalar_one()
    if total >= req.max_candidates_total:
        raise HTTPException(409, f"Sourcing-Limit erreicht: max_candidates_total={req.max_candidates_total}.")

    name_norm, domain_norm = _norm_name_domain(payload.company_name, payload.domain)

    # (1) Zuerst gegen bestehende/freigegebene Lieferanten (suppliers_v2) pruefen.
    existing_supplier = None
    r = await db.execute(select(SupplierV2).where(SupplierV2.tenant_id == membership.tenant_id))
    for s in r.scalars().all():
        if s.name.strip().lower() == name_norm:
            existing_supplier = s
            break

    # (3) Einfacher Duplikat-Abgleich gegen bereits erfasste Kandidaten
    # desselben Suchauftrags -- case-insensitive Name+Domain (dokumentierte
    # Vereinfachung, siehe B3 Punkt 3 im Playbook).
    duplicate_of = None
    r = await db.execute(select(SupplierCandidate).where(SupplierCandidate.sourcing_request_id == req.id))
    for existing in r.scalars().all():
        ex_name, ex_domain = _norm_name_domain(existing.company_name, existing.domain)
        if ex_name == name_norm and (not domain_norm or ex_domain == domain_norm):
            duplicate_of = existing
            break

    # (4) Muss-Kriterien NUR gegen dokumentierte Angaben pruefen -- fehlende
    # Information wird explizit "unknown", nie geraten.
    checks = {}
    open_questions = []
    for key in (req.must_criteria_json or {}).keys():
        checks[key] = "unknown"
        open_questions.append(f"Bitte bestaetigen Sie: {key} = {req.must_criteria_json.get(key)}?")

    candidate = SupplierCandidate(
        tenant_id=membership.tenant_id, sourcing_request_id=req.id,
        company_name=payload.company_name, domain=payload.domain, location=payload.location,
        service_match_note=payload.service_match_note, public_address=payload.public_address,
        contact_email=payload.contact_email, contact_name=payload.contact_name,
        source_url=payload.source_url, retrieved_at=payload.retrieved_at or datetime.utcnow(),
        existing_supplier_id=existing_supplier.id if existing_supplier else None,
        duplicate_of_id=duplicate_of.id if duplicate_of else None,
        must_criteria_check_json=checks, open_questions_json=open_questions,
        status=CandidateStatus.found, created_by=str(user.id),
    )
    db.add(candidate)
    await db.commit()
    await db.refresh(candidate)
    result = _candidate_to_dict(candidate)
    result["matched_existing_supplier"] = bool(existing_supplier)
    result["flagged_duplicate"] = bool(duplicate_of)
    return result


def _candidate_to_dict(c: SupplierCandidate) -> dict:
    return {
        "id": str(c.id), "sourcing_request_id": str(c.sourcing_request_id),
        "company_name": c.company_name, "domain": c.domain, "location": c.location,
        "service_match_note": c.service_match_note, "public_address": c.public_address,
        "contact_email": c.contact_email, "contact_name": c.contact_name,
        "source_url": c.source_url, "retrieved_at": c.retrieved_at,
        "existing_supplier_id": str(c.existing_supplier_id) if c.existing_supplier_id else None,
        "duplicate_of_id": str(c.duplicate_of_id) if c.duplicate_of_id else None,
        "must_criteria_check_json": c.must_criteria_check_json, "open_questions_json": c.open_questions_json,
        "status": c.status.value if hasattr(c.status, "value") else c.status,
        "stammblatt_json": c.stammblatt_json, "created_at": c.created_at,
    }


@router.get("/requests/{request_id}/candidates")
async def list_candidates(request_id: str, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    req = await _get_request_or_404(request_id, membership.tenant_id, db)
    r = await db.execute(select(SupplierCandidate).where(SupplierCandidate.sourcing_request_id == req.id).order_by(SupplierCandidate.created_at))
    return [_candidate_to_dict(c) for c in r.scalars().all()]


@router.get("/candidates/{candidate_id}")
async def get_candidate(candidate_id: str, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    cand = await _get_candidate_or_404(candidate_id, membership.tenant_id, db)
    return _candidate_to_dict(cand)


@router.put("/candidates/{candidate_id}/must-criteria")
async def update_must_criteria(
    candidate_id: str, payload: MustCriteriaUpdate,
    user=Depends(get_current_user), membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db),
):
    cand = await _get_candidate_or_404(candidate_id, membership.tenant_id, db)
    valid_values = {"met", "unmet", "unknown"}
    merged = dict(cand.must_criteria_check_json or {})
    for k, v in payload.checks.items():
        if v not in valid_values:
            raise HTTPException(400, f"Ungueltiger Pruefwert '{v}' fuer Kriterium '{k}'. Erlaubt: {sorted(valid_values)}.")
        merged[k] = v
    cand.must_criteria_check_json = merged
    cand.open_questions_json = [f"Bitte bestaetigen Sie: {k}" for k, v in merged.items() if v == "unknown"]
    await db.commit()
    return _candidate_to_dict(cand)


@router.post("/candidates/{candidate_id}/shortlist")
async def shortlist_candidate(
    candidate_id: str, user=Depends(get_current_user),
    membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db),
):
    """B3 Punkt 5: Shortlist = menschliche Pruefung VOR Kontaktaufnahme.
    Blockiert, solange noch ein Muss-Kriterium 'unmet' ist."""
    cand = await _get_candidate_or_404(candidate_id, membership.tenant_id, db)
    if cand.status != CandidateStatus.found:
        raise HTTPException(400, f"Kandidat im Status '{cand.status.value}' kann nicht auf die Shortlist gesetzt werden.")
    unmet = [k for k, v in (cand.must_criteria_check_json or {}).items() if v == "unmet"]
    if unmet:
        raise HTTPException(400, f"Muss-Kriterien nicht erfuellt: {unmet}. Kandidat kann nicht shortgelistet werden.")
    cand.status = CandidateStatus.shortlisted
    await db.commit()
    return _candidate_to_dict(cand)


@router.post("/candidates/{candidate_id}/reject")
async def reject_candidate(
    candidate_id: str, user=Depends(get_current_user),
    membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db),
):
    cand = await _get_candidate_or_404(candidate_id, membership.tenant_id, db)
    cand.status = CandidateStatus.rejected
    await db.commit()
    return _candidate_to_dict(cand)


# ---------------------------------------------------------------------------
# B4 -- Erstkontakt (Outreach)
# ---------------------------------------------------------------------------

@router.post("/candidates/{candidate_id}/outreach/draft")
async def draft_outreach(
    candidate_id: str, user=Depends(get_current_user),
    membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db),
):
    cand = await _get_candidate_or_404(candidate_id, membership.tenant_id, db)
    if cand.status not in (CandidateStatus.shortlisted,):
        raise HTTPException(400, f"Kandidat im Status '{cand.status.value}' ist nicht fuer den Erstkontakt bereit (muss 'shortlisted' sein).")
    if not cand.contact_email:
        raise HTTPException(400, "Kandidat hat keine contact_email hinterlegt.")

    req = await _get_request_or_404(str(cand.sourcing_request_id), membership.tenant_id, db)
    limit_problem = await _check_campaign_limits(req, db, about_to_contact=True)
    if limit_problem:
        raise HTTPException(409, limit_problem)

    vorgangs_id = str(cand.id)[:8]
    subject = FIRST_CONTACT_TEMPLATE_SUBJECT.format(bedarf=req.bedarf_text[:60], vorgangs_id=vorgangs_id)
    body = FIRST_CONTACT_TEMPLATE_BODY.format(bedarf=req.bedarf_text)

    action = OutreachAction(
        tenant_id=membership.tenant_id, supplier_candidate_id=cand.id, kind="first_contact",
        reminder_number=0, recipient_email=cand.contact_email, rendered_subject=subject,
        rendered_body=body, status=OutreachActionStatus.draft, created_by=str(user.id),
    )
    db.add(action)
    await db.commit()
    await db.refresh(action)
    return _action_to_dict(action)


def _action_to_dict(a: OutreachAction) -> dict:
    return {
        "id": str(a.id), "supplier_candidate_id": str(a.supplier_candidate_id), "kind": a.kind,
        "reminder_number": a.reminder_number, "recipient_email": a.recipient_email,
        "rendered_subject": a.rendered_subject, "rendered_body": a.rendered_body,
        "payload_hash": a.payload_hash, "status": a.status.value if hasattr(a.status, "value") else a.status,
        "created_at": a.created_at,
    }


@router.get("/candidates/{candidate_id}/outreach/{action_id}")
async def get_outreach_action(candidate_id: str, action_id: str, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    cand = await _get_candidate_or_404(candidate_id, membership.tenant_id, db)
    action = await _get_action_or_404(action_id, cand.id, db)
    return _action_to_dict(action)


@router.post("/candidates/{candidate_id}/outreach/{action_id}/approve")
async def approve_outreach(
    candidate_id: str, action_id: str,
    user=Depends(get_current_user), membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db),
):
    cand = await _get_candidate_or_404(candidate_id, membership.tenant_id, db)
    action = await _get_action_or_404(action_id, cand.id, db)

    if action.status == OutreachActionStatus.draft:
        h = _compute_hash(action.recipient_email, action.rendered_subject, action.rendered_body)
        action.payload_hash = h
        action.status = OutreachActionStatus.approved
        db.add(OutreachApproval(action_id=action.id, approved_by=str(user.id), payload_hash=h))
        await db.commit()
    elif action.status != OutreachActionStatus.approved:
        raise HTTPException(400, f"Aktion im Status '{action.status.value}' kann nicht freigegeben/gesendet werden.")

    await db.refresh(action)
    recomputed = _compute_hash(action.recipient_email, action.rendered_subject, action.rendered_body)
    if recomputed != action.payload_hash:
        raise HTTPException(400, "Freigabe ungueltig: Inhalt wurde nach Freigabe veraendert (Hash-Mismatch). Versand verweigert.")

    send_result = send_negotiation_email(to_email=action.recipient_email, subject=action.rendered_subject,
                                          body=action.rendered_body, from_name="NegotiateX.ai")
    if not send_result.get("sent"):
        raise HTTPException(502, f"Versand fehlgeschlagen: {send_result.get('message')}")

    msg_id = make_msgid(domain="negotiatex.ai")
    db.add(OutreachMessage(
        tenant_id=membership.tenant_id, supplier_candidate_id=cand.id, direction=OutreachDirection.outbound,
        message_id=msg_id, from_addr="info@negotiatex.ai", to_addr=action.recipient_email,
        subject=action.rendered_subject, body_text=action.rendered_body,
    ))
    action.status = OutreachActionStatus.sent
    if action.kind == "first_contact":
        cand.status = CandidateStatus.contacted
        req = (await db.execute(select(SourcingRequest).where(SourcingRequest.id == cand.sourcing_request_id))).scalar_one()
        schedule = req.reminder_schedule_minutes_json or [15, 40]
        now = datetime.utcnow()
        for i, minutes in enumerate(schedule[:2], start=1):
            db.add(OutreachReminderTimer(
                tenant_id=membership.tenant_id, supplier_candidate_id=cand.id,
                reminder_number=i, due_at=now + timedelta(minutes=minutes),
            ))
    await db.commit()
    return {"sent": True, "message_id": msg_id, "candidate_status": cand.status.value, "payload_hash": action.payload_hash}


@router.post("/candidates/{candidate_id}/outreach/{action_id}/reject")
async def reject_outreach(candidate_id: str, action_id: str, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    cand = await _get_candidate_or_404(candidate_id, membership.tenant_id, db)
    action = await _get_action_or_404(action_id, cand.id, db)
    if action.status not in (OutreachActionStatus.draft, OutreachActionStatus.approved):
        raise HTTPException(400, f"Aktion im Status '{action.status.value}' kann nicht abgelehnt werden.")
    action.status = OutreachActionStatus.rejected
    await db.commit()
    return _action_to_dict(action)


@router.get("/candidates/{candidate_id}/messages")
async def get_candidate_messages(candidate_id: str, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    cand = await _get_candidate_or_404(candidate_id, membership.tenant_id, db)
    r = await db.execute(select(OutreachMessage).where(OutreachMessage.supplier_candidate_id == cand.id).order_by(OutreachMessage.occurred_at))
    return [
        {"id": str(m.id), "direction": m.direction.value if hasattr(m.direction, "value") else m.direction,
         "message_id": m.message_id, "from_addr": m.from_addr, "to_addr": m.to_addr,
         "subject": m.subject, "body_text": m.body_text, "occurred_at": m.occurred_at}
        for m in r.scalars().all()
    ]


# ---------------------------------------------------------------------------
# Nutzeranforderung (9.10.2026): sobald ein Kandidat Interesse zeigt, soll das
# System proaktiv vorbereiten statt nur den Status zu setzen -- Stammdaten-
# Selbstauskunftslink, KI-Einschaetzung ob ein NDA noetig ist, und ein
# konkretes Briefing mit 3-Tage-Frist (RFQ), das um ein PDF-Angebot inkl.
# Firmendaten sowie Zusatzdokumente (Ratecards/Preislisten) bittet. ALLES
# hier Erzeugte ist ein DRAFT -- nichts wird automatisch versendet, jede
# Aktion braucht weiterhin die bestehende menschliche Freigabe (Hash-Bindung,
# siehe _compute_outreach_hash/approve-Endpunkte). Was automatisiert wird, ist
# ausschliesslich die VORBEREITUNG, nicht der tatsaechliche Versand.
# ---------------------------------------------------------------------------

STAMMDATEN_INVITE_SUBJECT_TMPL = "Vielen Dank fuer Ihr Interesse -- naechste Schritte / {vorgangs_id}"
STAMMDATEN_INVITE_BODY_TMPL = (
    "Guten Tag,\n\n"
    "vielen Dank fuer Ihre Rueckmeldung und Ihr Interesse. Als naechsten Schritt bitten wir Sie, "
    "Ihre Unternehmensdaten ueber den folgenden persoenlichen Link zu hinterlegen (ca. 5 Minuten):\n\n"
    "{link}\n\n"
    "Der Link ist nur fuer Sie bestimmt und laeuft nach einmaliger Nutzung ab.\n\n"
    "Freundliche Gruesse,\nNegotiateX – KI-gestuetzte Beschaffungskoordination.\n{testnote}"
)

RFQ_BRIEFING_SUBJECT_TMPL = "{prefix}Angebotsanfrage {bedarf} / {vorgangs_id}"
RFQ_BRIEFING_BODY_TMPL = (
    "Guten Tag,\n\n"
    "im Anschluss an Ihr Interesse erhalten Sie hiermit das konkrete Briefing zu unserer Anfrage:\n\n"
    "{bedarf}\n\n"
    "Spezifikation / Muss-Kriterien:\n{spec}\n\n"
    "Wir bitten Sie, bis zum {frist} ein Angebot abzugeben. Bitte reichen Sie Ihr Angebot als "
    "PDF-Dokument ein, das auch Ihre Firmendaten (Name, Anschrift, USt-IdNr.) enthaelt, sowie: "
    "Stueckpreis netto, Gesamtwarenwert, Fracht, weitere Kosten, Liefertermin, Zahlungsbedingungen "
    "und Angebotsgueltigkeit. Bestaetigen Sie die Spezifikation ausdruecklich und kennzeichnen Sie "
    "jede Abweichung.\n\n"
    "Gerne koennen Sie zusaetzlich zu Ihrem Angebot weitere Unterlagen beifuegen, die uns die "
    "Bewertung erleichtern -- z.B. Ratecards, Preislisten oder Referenzprojekte.\n\n"
    "{nda_note}"
    "Diese Anfrage begruendet keine Bestellung.\n\n"
    "Freundliche Gruesse,\nNegotiateX – KI-gestuetzte Beschaffungskoordination.\n{testnote}"
)
RFQ_NDA_PENDING_NOTE = (
    "Da diese Anfrage vertrauliche Informationen beruehrt, erhalten Sie zunaechst eine "
    "Vertraulichkeitsvereinbarung (NDA) zur Pruefung und Unterzeichnung; das vollstaendige "
    "Briefing folgt im Anschluss daran.\n\n"
)


async def _auto_prepare_after_interest(db: AsyncSession, cand: SupplierCandidate, tenant_id) -> None:
    """Siehe Moduldoc oben. Wird aus dem 'interest'-Zweig von
    ingest_inbound_outreach_message aufgerufen, NACH dem commit von
    Statusaenderung/stammblatt_json (damit diese Funktion bei einem Fehler
    die eigentliche Klassifikation/den Status-Uebergang nicht gefaehrdet --
    sie ist additive Vorbereitung, kein kritischer Pfad)."""
    req = (await db.execute(select(SourcingRequest).where(SourcingRequest.id == cand.sourcing_request_id))).scalar_one_or_none()
    if not req or not cand.contact_email:
        return
    vorgangs_id = str(cand.id)[:8]

    # 1) Stammdaten-Selbstauskunftslink (einmalig pro Kandidat).
    existing_invite = await db.execute(select(CandidateOnboardingInvite).where(CandidateOnboardingInvite.supplier_candidate_id == cand.id))
    if not existing_invite.scalars().first():
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        db.add(CandidateOnboardingInvite(tenant_id=tenant_id, supplier_candidate_id=cand.id, token_hash=token_hash))
        link = f"https://negotiatex.ai/candidate-onboarding?token={raw_token}"
        db.add(OutreachAction(
            tenant_id=tenant_id, supplier_candidate_id=cand.id, kind="stammdaten_invite",
            recipient_email=cand.contact_email,
            rendered_subject=STAMMDATEN_INVITE_SUBJECT_TMPL.format(vorgangs_id=vorgangs_id),
            rendered_body=STAMMDATEN_INVITE_BODY_TMPL.format(link=link, testnote="Testlauf, keine Beauftragung."),
            status=OutreachActionStatus.draft, created_by="system",
        ))

    # 2) KI-Einschaetzung: ist ein NDA vor dem vollen Briefing sinnvoll?
    #    Vorschlag, kein autonomer Beschluss -- siehe require_nda_approved().
    if cand.nda_assessment_json is None:
        must_criteria_text = "; ".join(str(v) for v in (cand.must_criteria_check_json or {}).values()) or (req.must_criteria_json and str(req.must_criteria_json)) or ""
        assessment = assess_nda_necessity(
            bedarf_text=req.bedarf_text, must_criteria_text=must_criteria_text,
            confidential_notice=req.confidential_notice, public_teaser_text=req.public_teaser_text,
        )
        assessment["assessed_at"] = datetime.utcnow().isoformat()
        cand.nda_assessment_json = assessment
    needs_nda = bool((cand.nda_assessment_json or {}).get("needs_nda", True))

    # 3) RFQ fuer diesen Suchauftrag sicherstellen (3-Tage-Frist, idempotent).
    existing_rfq = await db.execute(select(RFQ).where(RFQ.sourcing_request_id == req.id))
    rfq = existing_rfq.scalars().first()
    if not rfq:
        spec_parts = [req.bedarf_text]
        if req.must_criteria_json:
            spec_parts.append(f"Muss-Kriterien: {req.must_criteria_json}")
        rfq = RFQ(
            tenant_id=tenant_id, sourcing_request_id=req.id, spec_text="\n".join(spec_parts),
            currency=req.currency, deadline=datetime.utcnow() + timedelta(days=3), test_mode=True,
            created_by="system",
        )
        db.add(rfq)
        await db.flush()

    if needs_nda:
        # 4a) NDA-Pfad: Entwurf vorbereiten (identische Kernlogik wie der
        # manuelle /nda/draft-Endpunkt), RFQ-Einladung folgt erst nach
        # menschlicher NDA-Freigabe (require_nda_approved blockt das ohnehin).
        existing_nda = await db.execute(select(NDA).where(NDA.supplier_candidate_id == cand.id))
        if not existing_nda.scalars().first():
            sb = cand.stammblatt_json or {}
            signatory = (sb.get("unterzeichnungsberechtigt_vertrag") or {}).get("value")
            authorized_confirmed = bool(signatory) and sb.get("unterzeichnungsberechtigt_vertrag", {}).get("status") in ("geprueft", "freigegeben")
            draft_text = NDA_TEMPLATE_TEXT.format(
                party_a="Auftraggeber (vertreten durch NegotiateX.ai)", party_b=cand.company_name,
                bedarf=cand.service_match_note or cand.company_name, signatory=signatory or "(noch nicht benannt)",
            )
            nda = NDA(
                tenant_id=tenant_id, supplier_candidate_id=cand.id, party_b_name=cand.company_name,
                signatory_name=signatory, signatory_authorized_confirmed=authorized_confirmed,
                draft_text=draft_text, draft_hash=_compute_hash(draft_text), status=NDAStatus.draft,
                created_by="system",
            )
            db.add(nda)
            await db.flush()
            db.add(NDAEvent(nda_id=nda.id, tenant_id=tenant_id, from_status=None, to_status=NDAStatus.draft.value,
                             actor="system", reason="Automatisch vorbereitet nach KI-Einschaetzung needs_nda=true (Interesse-Antwort)."))
            cand.status = CandidateStatus.nda_review
    else:
        # 4b) Kein NDA noetig: RFQ-Einladung mit vollem Briefing direkt entwerfen.
        existing_rfq_invite = await db.execute(select(RFQInvitation).where(RFQInvitation.rfq_id == rfq.id, RFQInvitation.supplier_candidate_id == cand.id))
        if not existing_rfq_invite.scalars().first():
            db.add(RFQInvitation(tenant_id=tenant_id, rfq_id=rfq.id, supplier_candidate_id=cand.id))
            subject = RFQ_BRIEFING_SUBJECT_TMPL.format(prefix="[TEST – ]", bedarf=req.bedarf_text[:60], vorgangs_id=vorgangs_id)
            body = RFQ_BRIEFING_BODY_TMPL.format(
                bedarf=req.bedarf_text, spec=rfq.spec_text, frist=rfq.deadline.strftime("%d.%m.%Y"),
                nda_note="", testnote="Testlauf, keine Beauftragung.",
            )
            db.add(RFQAction(
                tenant_id=tenant_id, rfq_id=rfq.id, supplier_candidate_id=cand.id, kind="invite",
                recipient_email=cand.contact_email, rendered_subject=subject, rendered_body=body,
                status=RFQActionStatus.draft, created_by="system",
            ))


# ---------------------------------------------------------------------------
# Inbound outreach reply handling -- shared by IMAP poller
# (services/email_poller.py) and the test-inject endpoint below.
# ---------------------------------------------------------------------------

async def ingest_inbound_outreach_message(db: AsyncSession, email_record: dict, candidate_override: "SupplierCandidate | None" = None) -> dict:
    """Analog zu routers.negotiation.ingest_inbound_message. Matched via
    Message-ID-Header gegen gespeicherte outbound OutreachMessage-Zeilen.
    JEDE eingehende Antwort (gleich welcher Klassifikation) storniert alle
    offenen Reminder-Timer fuer diesen Kandidaten sofort (B4).

    `candidate_override`: gesetzt vom Subject-Token-Fallback in
    email_poller.py, wenn der echte Mailserver (bestaetigt: Strato/rzone)
    die Message-ID beim Versand umschreibt und die Header-Zuordnung
    deshalb leerlaeuft. Wenn gesetzt, wird direkt dieser Kandidat
    verwendet statt ueber eine OutreachMessage-Zeile aufgeloest."""
    in_reply_to = email_record.get("in_reply_to")
    refs = email_record.get("references_header") or ""

    if candidate_override is not None:
        cand = candidate_override
        tenant_id = cand.tenant_id
    else:
        candidate_ids = set()
        if in_reply_to:
            candidate_ids.add(in_reply_to.strip())
        for token in refs.split():
            candidate_ids.add(token.strip())

        matched_outbound = None
        if candidate_ids:
            r = await db.execute(
                select(OutreachMessage).where(
                    OutreachMessage.direction == OutreachDirection.outbound,
                    OutreachMessage.message_id.in_(list(candidate_ids)),
                )
            )
            matched_outbound = r.scalars().first()

        if not matched_outbound:
            return {"matched": False}

        cand_id = matched_outbound.supplier_candidate_id
        tenant_id = matched_outbound.tenant_id
        r = await db.execute(select(SupplierCandidate).where(SupplierCandidate.id == cand_id))
        cand = r.scalar_one_or_none()
        if not cand:
            return {"matched": False}

    if email_record.get("message_id"):
        existing = await db.execute(select(OutreachMessage).where(OutreachMessage.message_id == email_record["message_id"]))
        if existing.scalar_one_or_none():
            return {"matched": True, "duplicate": True}

    from_addr = (email_record.get("from_addr") or "").strip().lower()
    inbound = OutreachMessage(
        tenant_id=tenant_id, supplier_candidate_id=cand.id, direction=OutreachDirection.inbound,
        message_id=email_record.get("message_id") or f"<generated-{uuid.uuid4()}@negotiatex.ai>",
        in_reply_to=in_reply_to, references_header=refs or None,
        from_addr=from_addr, to_addr=email_record.get("to_addr") or "info@negotiatex.ai",
        subject=email_record.get("subject"), body_text=email_record.get("body_text") or "",
        raw_source=email_record.get("raw_source"),
    )
    db.add(inbound)
    await db.flush()

    # Jede Antwort storniert alle offenen Reminder-Timer fuer diesen Kandidaten.
    r = await db.execute(select(OutreachReminderTimer).where(
        OutreachReminderTimer.supplier_candidate_id == cand.id, OutreachReminderTimer.cancelled == False,  # noqa: E712
        OutreachReminderTimer.fired == False,  # noqa: E712
    ))
    for t in r.scalars().all():
        t.cancelled = True

    cand.status = CandidateStatus.responded
    classification = classify_outreach_reply(inbound.body_text)

    if classification == "decline":
        cand.status = CandidateStatus.declined
    elif classification == "interest":
        cand.status = CandidateStatus.interested
        # Interesse -> Ansprechpartner als erstes Stammblatt-Feld erfassen,
        # dann weiter zur Aufnahme.
        sb = dict(cand.stammblatt_json or {})
        sb["ansprechpartner_bestaetigt"] = {
            "status": "eingegangen", "source": "outreach_reply", "reviewer": None,
            "value": email_record.get("from_addr"), "updated_at": datetime.utcnow().isoformat(),
        }
        cand.stammblatt_json = sb
        cand.status = CandidateStatus.onboarding_pending
        await db.flush()
        try:
            await _auto_prepare_after_interest(db, cand, tenant_id)
        except Exception:
            logger.exception(f"_auto_prepare_after_interest fehlgeschlagen fuer Kandidat {cand.id} -- Status/Klassifikation bleiben trotzdem gueltig, manuelle Nachbereitung noetig.")
    elif classification == "question":
        # Rueckfrage: NUR aus dem bestaetigten must_criteria_text der
        # SourcingRequest beantworten, nie erfinden. Ohne eindeutigen
        # Treffer wird eskaliert (Kandidat bleibt 'responded' -> erscheint
        # im Dashboard zur manuellen Pruefung).
        cand.open_questions_json = list(cand.open_questions_json or []) + [
            f"Rueckfrage erhalten, erfordert menschliche Antwort: {inbound.body_text[:300]}"
        ]
    else:  # no_signal
        cand.open_questions_json = list(cand.open_questions_json or []) + [
            "Antwort nicht eindeutig klassifizierbar -- manuelle Pruefung erforderlich."
        ]

    await db.commit()
    return {"matched": True, "classification": classification, "candidate_status": cand.status.value}


@router.post("/candidates/{candidate_id}/prepare-followup")
async def prepare_followup(
    candidate_id: str, user=Depends(get_current_user),
    membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db),
):
    """Manueller Nachtrag fuer Kandidaten, bei denen die automatische
    Vorbereitung (Stammdaten-Einladung, NDA-Einschaetzung, RFQ-Briefing) nicht
    gelaufen ist -- z.B. weil der Kandidat schon vor Einfuehrung dieser
    Funktion Interesse bekundet hatte. Ruft dieselbe, idempotente Funktion wie
    der automatische Pfad auf (ueberspringt bereits vorhandene Einladung/NDA/
    RFQ-Einladung), erzeugt also nur, was wirklich noch fehlt."""
    cand = await _get_candidate_or_404(candidate_id, membership.tenant_id, db)
    if not cand.contact_email:
        raise HTTPException(400, "Kandidat hat keine contact_email hinterlegt.")
    await _auto_prepare_after_interest(db, cand, membership.tenant_id)
    await db.commit()
    await db.refresh(cand)
    return _candidate_to_dict(cand)


@router.get("/candidates/{candidate_id}/outreach")
async def list_outreach_actions(candidate_id: str, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    cand = await _get_candidate_or_404(candidate_id, membership.tenant_id, db)
    r = await db.execute(select(OutreachAction).where(OutreachAction.supplier_candidate_id == cand.id).order_by(desc(OutreachAction.created_at)))
    return [_action_to_dict(a) for a in r.scalars().all()]


@router.get("/candidates/{candidate_id}/nda")
async def get_nda_for_candidate(candidate_id: str, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    cand = await _get_candidate_or_404(candidate_id, membership.tenant_id, db)
    r = await db.execute(select(NDA).where(NDA.supplier_candidate_id == cand.id))
    nda = r.scalar_one_or_none()
    if not nda:
        raise HTTPException(404, "Fuer diesen Kandidaten existiert noch keine NDA.")
    return _nda_to_dict(nda)


@router.get("/candidates/{candidate_id}/onboarding-invite")
async def get_onboarding_invite(candidate_id: str, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    cand = await _get_candidate_or_404(candidate_id, membership.tenant_id, db)
    r = await db.execute(select(CandidateOnboardingInvite).where(CandidateOnboardingInvite.supplier_candidate_id == cand.id))
    inv = r.scalar_one_or_none()
    if not inv:
        raise HTTPException(404, "Fuer diesen Kandidaten existiert noch keine Stammdaten-Einladung.")
    return {
        "id": str(inv.id), "status": inv.status.value if hasattr(inv.status, "value") else inv.status,
        "confirmed_by_name": inv.confirmed_by_name, "confirmed_at": inv.confirmed_at, "created_at": inv.created_at,
    }


@router.post("/internal/test-inbound-outreach/{candidate_id}")
async def test_inbound_outreach(
    candidate_id: str, payload: TestInboundOutreachReply,
    membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db),
):
    """NUR fuer Tests/Verifikation -- simuliert exakt das, was der IMAP-Poller
    aus einer echten Outreach-Antwort erzeugen wuerde (siehe
    negotiation.py's analoger Endpunkt)."""
    cand = await _get_candidate_or_404(candidate_id, membership.tenant_id, db)
    r = await db.execute(
        select(OutreachMessage)
        .where(OutreachMessage.supplier_candidate_id == cand.id, OutreachMessage.direction == OutreachDirection.outbound)
        .order_by(desc(OutreachMessage.occurred_at))
    )
    last_outbound = r.scalars().first()
    email_record = {
        "message_id": f"<test-inbound-{uuid.uuid4()}@example-supplier.test>",
        "in_reply_to": last_outbound.message_id if last_outbound else None,
        "references_header": last_outbound.message_id if last_outbound else None,
        "from_addr": payload.from_addr, "to_addr": "info@negotiatex.ai",
        "subject": payload.subject, "body_text": payload.body_text,
        "raw_source": "SIMULATED-TEST-INBOUND",
    }
    return await ingest_inbound_outreach_message(db, email_record)


# ---------------------------------------------------------------------------
# B5 -- Stammblatt (Lieferantenaufnahme)
# ---------------------------------------------------------------------------

VALID_FIELD_STATUS = {"fehlt", "eingegangen", "geprueft", "freigegeben"}


@router.put("/candidates/{candidate_id}/stammblatt")
async def update_stammblatt_field(
    candidate_id: str, payload: StammblattFieldUpdate,
    user=Depends(get_current_user), membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db),
):
    cand = await _get_candidate_or_404(candidate_id, membership.tenant_id, db)
    if payload.status not in VALID_FIELD_STATUS:
        raise HTTPException(400, f"Ungueltiger Status. Erlaubt: {sorted(VALID_FIELD_STATUS)}.")
    sb = dict(cand.stammblatt_json or {})
    sb[payload.field_name] = {
        "value": payload.value, "status": payload.status, "source": payload.source,
        "reviewer": payload.reviewer or str(user.id), "updated_at": datetime.utcnow().isoformat(),
    }
    cand.stammblatt_json = sb
    await db.commit()
    return _candidate_to_dict(cand)


@router.post("/candidates/{candidate_id}/promote-to-supplier")
async def promote_to_supplier(
    candidate_id: str, user=Depends(get_current_user),
    membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db),
):
    """Legt (oder aktualisiert) den tenant-gescopten suppliers_v2-Eintrag aus
    den bislang erfassten Stammblatt-Daten an. Zahlungsdaten (IBAN/BIC)
    werden HIER NICHT gesetzt -- diese laufen ausschliesslich ueber die
    eigenen /bank-data-Endpunkte unten, nie ueber das Outreach-Mail-Template
    (B5-Zitat: 'Bankdaten werden nicht per gewoehnlicher E-Mail angefordert')."""
    cand = await _get_candidate_or_404(candidate_id, membership.tenant_id, db)
    if cand.existing_supplier_id:
        supplier = await _get_supplier_or_404(str(cand.existing_supplier_id), membership.tenant_id, db)
    else:
        supplier = SupplierV2(
            tenant_id=membership.tenant_id, name=cand.company_name, category=cand.service_match_note,
            contact_name=cand.contact_name, email=cand.contact_email, address=cand.public_address,
            country=None, tax_id=None,
        )
        db.add(supplier)
        await db.flush()
        cand.existing_supplier_id = supplier.id
    await db.commit()
    await db.refresh(supplier)
    return {"supplier_id": str(supplier.id), "candidate_id": str(cand.id)}


@router.put("/suppliers/{supplier_id}/bank-data")
async def update_bank_data(
    supplier_id: str, payload: BankDataUpdate,
    user=Depends(get_current_user), membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db),
):
    """B5-Zitat, woertlich implementiert: 'Eine Aenderung von Bankdaten
    loest eine unabhaengige Verifikation aus.' Jede tatsaechliche Aenderung
    an IBAN/BIC gegenueber dem bisherigen Wert setzt bank_data_verified
    zurueck auf False -- unabhaengig davon, ob vorher schon einmal verifiziert
    wurde. Der Datensatz ist danach fuer Zahlungszwecke erst wieder
    nutzbar, nachdem /bank-data/verify erneut explizit aufgerufen wurde."""
    supplier = await _get_supplier_or_404(supplier_id, membership.tenant_id, db)
    changed = False
    if payload.iban is not None and payload.iban != supplier.iban:
        supplier.iban = payload.iban
        changed = True
    if payload.bic is not None and payload.bic != supplier.bic:
        supplier.bic = payload.bic
        changed = True
    if payload.bank_name is not None and payload.bank_name != supplier.bank_name:
        supplier.bank_name = payload.bank_name

    if changed:
        supplier.bank_data_verified = False
        supplier.bank_data_verified_at = None

    await db.commit()
    await db.refresh(supplier)
    return {
        "supplier_id": str(supplier.id), "iban": supplier.iban, "bic": supplier.bic,
        "bank_name": supplier.bank_name, "bank_data_verified": supplier.bank_data_verified,
        "re_verification_required": changed,
    }


@router.post("/suppliers/{supplier_id}/bank-data/verify")
async def verify_bank_data(
    supplier_id: str, payload: VerifyPayload,
    user=Depends(get_current_user), membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db),
):
    """Menschlicher Verifikationsschritt -- NIE automatisch gesetzt."""
    supplier = await _get_supplier_or_404(supplier_id, membership.tenant_id, db)
    if not supplier.iban:
        raise HTTPException(400, "Keine Bankdaten hinterlegt -- nichts zu verifizieren.")
    supplier.bank_data_verified = True
    supplier.bank_data_verified_at = datetime.utcnow()
    await db.commit()
    return {"supplier_id": str(supplier.id), "bank_data_verified": True, "verified_by": payload.verified_by or str(user.id)}


@router.post("/candidates/{candidate_id}/certificates")
async def add_certificate(
    candidate_id: str, payload: CertificateCreate,
    user=Depends(get_current_user), membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db),
):
    cand = await _get_candidate_or_404(candidate_id, membership.tenant_id, db)
    doc_uuid = uuid.UUID(payload.document_id) if payload.document_id else None
    cert = SupplierCertificate(
        tenant_id=membership.tenant_id, supplier_candidate_id=cand.id,
        name=payload.name, issuer=payload.issuer, valid_until=payload.valid_until,
        document_id=doc_uuid, verified=False,
    )
    db.add(cert)
    await db.commit()
    await db.refresh(cert)
    return _cert_to_dict(cert)


def _cert_to_dict(c: SupplierCertificate) -> dict:
    return {
        "id": str(c.id), "name": c.name, "issuer": c.issuer, "valid_until": c.valid_until,
        "document_id": str(c.document_id) if c.document_id else None,
        "verified": c.verified, "verified_by": c.verified_by, "verified_at": c.verified_at,
    }


@router.get("/candidates/{candidate_id}/certificates")
async def list_certificates(candidate_id: str, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    cand = await _get_candidate_or_404(candidate_id, membership.tenant_id, db)
    r = await db.execute(select(SupplierCertificate).where(SupplierCertificate.supplier_candidate_id == cand.id))
    return [_cert_to_dict(c) for c in r.scalars().all()]


@router.post("/certificates/{certificate_id}/verify")
async def verify_certificate(
    certificate_id: str, payload: VerifyPayload,
    user=Depends(get_current_user), membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db),
):
    """'Ein angehaengtes PDF allein beweist keine Eignung' -- dieser
    Endpunkt ist der EINZIGE Code-Pfad, der verified=True setzen kann."""
    try:
        cid = uuid.UUID(certificate_id)
    except ValueError:
        raise HTTPException(404, "Zertifikat nicht gefunden.")
    r = await db.execute(select(SupplierCertificate).where(SupplierCertificate.id == cid))
    cert = r.scalar_one_or_none()
    if not cert or cert.tenant_id != membership.tenant_id:
        raise HTTPException(404, "Zertifikat nicht gefunden.")
    cert.verified = True
    cert.verified_by = payload.verified_by or str(user.id)
    cert.verified_at = datetime.utcnow()
    await db.commit()
    return _cert_to_dict(cert)


# ---------------------------------------------------------------------------
# B6 -- NDA Workflow
# ---------------------------------------------------------------------------

NDA_TEMPLATE_TEXT = (
    "GEHEIMHALTUNGSVEREINBARUNG (Entwurf, Vorlage standard_v1)\n\n"
    "Zwischen {party_a} und {party_b}.\n\n"
    "Gegenstand: Austausch vertraulicher Informationen im Rahmen der Beschaffungsanfrage "
    "'{bedarf}'. Der Empfaenger verpflichtet sich, alle als vertraulich gekennzeichneten "
    "Informationen ausschliesslich zum Zweck der Angebotserstellung zu verwenden und nicht an "
    "Dritte weiterzugeben.\n\nUnterzeichnung durch: {signatory}.\n\n"
    "Dies ist ein Vorlagentext zu Testzwecken, keine rechtsverbindliche Ausfertigung."
)


@router.post("/candidates/{candidate_id}/nda/draft")
async def draft_nda(
    candidate_id: str, user=Depends(get_current_user),
    membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db),
):
    """B6 Schritt 1 (Entwurf). Ein Mensch muss zusaetzlich pruefen, wenn der
    Signatar noch nicht als berechtigt bestaetigt ist (hier: Warnung im
    Response statt hartem Block, da das Entwerfen selbst noch keine Wirkung
    nach aussen hat -- erst /send setzt tatsaechlich etwas in Gang)."""
    cand = await _get_candidate_or_404(candidate_id, membership.tenant_id, db)
    existing = await db.execute(select(NDA).where(NDA.supplier_candidate_id == cand.id))
    if existing.scalar_one_or_none():
        raise HTTPException(400, "Fuer diesen Kandidaten existiert bereits eine NDA.")

    signatory = None
    sb = cand.stammblatt_json or {}
    if "unterzeichnungsberechtigt_vertrag" in sb:
        signatory = sb["unterzeichnungsberechtigt_vertrag"].get("value")
    authorized_confirmed = bool(signatory) and sb.get("unterzeichnungsberechtigt_vertrag", {}).get("status") in ("geprueft", "freigegeben")

    draft_text = NDA_TEMPLATE_TEXT.format(
        party_a="Auftraggeber (vertreten durch NegotiateX.ai)", party_b=cand.company_name,
        bedarf=cand.service_match_note or cand.company_name, signatory=signatory or "(noch nicht benannt)",
    )
    draft_hash = _compute_hash(draft_text)

    nda = NDA(
        tenant_id=membership.tenant_id, supplier_candidate_id=cand.id,
        party_b_name=cand.company_name, signatory_name=signatory,
        signatory_authorized_confirmed=authorized_confirmed,
        draft_text=draft_text, draft_hash=draft_hash, status=NDAStatus.draft, created_by=str(user.id),
    )
    db.add(nda)
    await db.flush()
    db.add(NDAEvent(nda_id=nda.id, tenant_id=membership.tenant_id, from_status=None, to_status=NDAStatus.draft.value,
                     actor=str(user.id), reason="NDA-Entwurf erstellt."))
    cand.status = CandidateStatus.nda_review
    await db.commit()
    await db.refresh(nda)
    result = _nda_to_dict(nda)
    result["review_required_reason"] = None if authorized_confirmed else (
        "Signatar ist noch nicht als unterzeichnungsberechtigt bestaetigt -- menschliche Pruefung vor Versand erforderlich."
    )
    return result


def _nda_to_dict(n: NDA) -> dict:
    return {
        "id": str(n.id), "supplier_candidate_id": str(n.supplier_candidate_id),
        "template_version": n.template_version, "party_a_name": n.party_a_name, "party_b_name": n.party_b_name,
        "signatory_name": n.signatory_name, "signatory_authorized_confirmed": n.signatory_authorized_confirmed,
        "draft_text": n.draft_text, "draft_hash": n.draft_hash,
        "status": n.status.value if hasattr(n.status, "value") else n.status,
        "sent_at": n.sent_at, "returned_at": n.returned_at, "redline_detected": n.redline_detected,
        "apparent_signature_claim": n.apparent_signature_claim,
        "verified": n.verified, "verified_by": n.verified_by, "verified_at": n.verified_at,
        "verification_checklist_json": n.verification_checklist_json,
        "approved_by": n.approved_by, "approved_at": n.approved_at,
    }


@router.get("/nda/{nda_id}")
async def get_nda(nda_id: str, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    nda = await _get_nda_or_404(nda_id, membership.tenant_id, db)
    return _nda_to_dict(nda)


@router.get("/nda/{nda_id}/events")
async def get_nda_events(nda_id: str, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    nda = await _get_nda_or_404(nda_id, membership.tenant_id, db)
    r = await db.execute(select(NDAEvent).where(NDAEvent.nda_id == nda.id).order_by(NDAEvent.created_at))
    return [
        {"id": str(e.id), "from_status": e.from_status, "to_status": e.to_status, "actor": e.actor,
         "reason": e.reason, "created_at": e.created_at}
        for e in r.scalars().all()
    ]


@router.post("/nda/{nda_id}/approve-send")
async def approve_send_nda(
    nda_id: str, user=Depends(get_current_user),
    membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db),
):
    """B6 Schritt 2 (Zustellung). Identisches Hash-Bindungsmuster wie
    negotiation.py's approve_action: der Freigabe-Hash wird unmittelbar vor
    dem tatsaechlichen Versand aus dem aktuellen draft_text neu berechnet."""
    nda = await _get_nda_or_404(nda_id, membership.tenant_id, db)
    if nda.status != NDAStatus.draft:
        raise HTTPException(400, f"NDA im Status '{nda.status.value}' kann nicht (erneut) zum Versand freigegeben werden.")

    recomputed = _compute_hash(nda.draft_text)
    if recomputed != nda.draft_hash:
        raise HTTPException(400, "Freigabe ungueltig: Entwurfstext wurde nach Erstellung veraendert (Hash-Mismatch). Versand verweigert.")

    cand = (await db.execute(select(SupplierCandidate).where(SupplierCandidate.id == nda.supplier_candidate_id))).scalar_one()
    if not cand.contact_email:
        raise HTTPException(400, "Kandidat hat keine contact_email hinterlegt.")

    subject = f"[TEST] NDA-Entwurf zur Pruefung -- {nda.party_b_name}"
    send_result = send_negotiation_email(to_email=cand.contact_email, subject=subject, body=nda.draft_text, from_name="NegotiateX.ai")
    if not send_result.get("sent"):
        raise HTTPException(502, f"Versand fehlgeschlagen: {send_result.get('message')}")

    msg_id = make_msgid(domain="negotiatex.ai")
    db.add(OutreachMessage(
        tenant_id=membership.tenant_id, supplier_candidate_id=cand.id, direction=OutreachDirection.outbound,
        message_id=msg_id, from_addr="info@negotiatex.ai", to_addr=cand.contact_email,
        subject=subject, body_text=nda.draft_text,
    ))
    nda.sent_at = datetime.utcnow()
    nda.sent_message_id = msg_id
    nda.sent_payload_hash = recomputed
    await _nda_transition(db, nda, NDAStatus.sent, actor=str(user.id), reason="NDA-Entwurf versendet (menschlich freigegeben).")
    await db.commit()
    return {"sent": True, "message_id": msg_id, "nda_status": nda.status.value}


@router.post("/nda/{nda_id}/return")
async def return_nda(
    nda_id: str, payload: NDAReturnPayload,
    user=Depends(get_current_user), membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db),
):
    """B6 Schritt 3 (Ruecklauf). Vergleicht den zurueckgekommenen Text gegen
    den versendeten draft_text -- JEDE erkannte Aenderung routet auf
    review_required, NIE automatische Zustimmung (Playbook-Zitat: 'Redlines
    an Legal; keine automatische Zustimmung.'). apparent_signature_claim ist
    nur ein Anzeige-Signal und setzt NIE selbst einen Status."""
    nda = await _get_nda_or_404(nda_id, membership.tenant_id, db)
    if nda.status != NDAStatus.sent:
        raise HTTPException(400, f"NDA im Status '{nda.status.value}' erwartet keinen Ruecklauf.")

    redline = detect_redline(nda.draft_text, payload.returned_text)
    sig_claim = detect_signature_claim(payload.returned_text)

    nda.returned_at = datetime.utcnow()
    nda.returned_text = payload.returned_text
    nda.returned_hash = _compute_hash(payload.returned_text)
    nda.redline_detected = redline
    nda.apparent_signature_claim = sig_claim

    if redline:
        await _nda_transition(db, nda, NDAStatus.review_required, actor="system",
                               reason="Ruecklauf weicht vom versendeten Text ab (Redline erkannt) -- an Legal/Human-Review, keine automatische Zustimmung.")
    else:
        await _nda_transition(db, nda, NDAStatus.returned, actor="system",
                               reason="Ruecklauf identisch zum versendeten Text -- bereit fuer manuelle Pruefung (/verify)."
                               + (" Hinweis: Text enthaelt eine Unterschriften-Behauptung, dies allein setzt KEINEN Status." if sig_claim else ""))
    await db.commit()
    result = _nda_to_dict(nda)
    result["note"] = "apparent_signature_claim ist nur ein Textsignal und oeffnet niemals von selbst den Zugriff."
    return result


@router.post("/nda/{nda_id}/verify")
async def verify_nda(
    nda_id: str, payload: NDAVerifyPayload,
    user=Depends(get_current_user), membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db),
):
    """B6 Schritt 4 (Pruefung). Rein menschlich ausgeloest. Setzt verified=True
    NUR, wenn alle vier Teilpruefungen bestaetigt sind; sonst bleibt der
    Status unveraendert (review_required/returned) und der Grund wird
    zurueckgegeben -- kein Teilerfolg wird automatisch zu 'approved'."""
    nda = await _get_nda_or_404(nda_id, membership.tenant_id, db)
    if nda.status not in (NDAStatus.returned, NDAStatus.review_required):
        raise HTTPException(400, f"NDA im Status '{nda.status.value}' kann nicht geprueft werden (Ruecklauf erforderlich).")

    checklist = {
        "version_matches": payload.version_matches, "parties_match": payload.parties_match,
        "signatory_authorized": payload.signatory_authorized, "countersignature_present": payload.countersignature_present,
    }
    nda.verification_checklist_json = checklist
    all_ok = all(checklist.values())

    if not all_ok:
        failed = [k for k, v in checklist.items() if not v]
        raise HTTPException(400, f"Pruefung nicht vollstaendig bestanden ({failed}) -- Status bleibt '{nda.status.value}'. "
                                   f"Grund/Notiz: {payload.note or 'keine'}")

    nda.verified = True
    nda.verified_by = payload.verified_by or str(user.id)
    nda.verified_at = datetime.utcnow()
    await _nda_transition(db, nda, NDAStatus.verified, actor=str(user.id), reason="Menschliche Pruefung bestanden (Version/Parteien/Signatar/Gegenzeichnung).")
    await db.commit()
    return _nda_to_dict(nda)


@router.post("/nda/{nda_id}/approve")
async def approve_nda(
    nda_id: str, user=Depends(get_current_user),
    membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db),
):
    """B6 Schritt 5 (Zugang) -- Gate. Dies ist der EINZIGE Code-Pfad im
    gesamten System, der NDA.status auf 'approved' setzen kann, und er
    verlangt zwingend vorheriges status==verified (also einen vorherigen,
    separaten menschlichen /verify-Aufruf mit vollstaendig bestandener
    Checkliste). Eine blosse Behauptung 'ist unterschrieben' im Rueck-
    meldetext kann diesen Zustand NIE erreichen, da sie hoechstens
    apparent_signature_claim setzt, niemals verified/status."""
    nda = await _get_nda_or_404(nda_id, membership.tenant_id, db)
    if nda.status != NDAStatus.verified or not nda.verified:
        raise HTTPException(400, "NDA kann nur nach bestandener menschlicher Pruefung (/verify, status=verified) freigegeben werden.")

    await _nda_transition(db, nda, NDAStatus.approved, actor=str(user.id), reason="NDA final freigegeben (menschliche Entscheidung).")
    nda.approved_by = str(user.id)
    nda.approved_at = datetime.utcnow()

    cand = (await db.execute(select(SupplierCandidate).where(SupplierCandidate.id == nda.supplier_candidate_id))).scalar_one()
    cand.status = CandidateStatus.nda_approved
    await db.commit()
    return _nda_to_dict(nda)


@router.post("/nda/{nda_id}/reject")
async def reject_nda(
    nda_id: str, user=Depends(get_current_user),
    membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db),
):
    nda = await _get_nda_or_404(nda_id, membership.tenant_id, db)
    await _nda_transition(db, nda, NDAStatus.rejected, actor=str(user.id), reason="NDA abgelehnt (menschliche Entscheidung).")
    cand = (await db.execute(select(SupplierCandidate).where(SupplierCandidate.id == nda.supplier_candidate_id))).scalar_one()
    cand.status = CandidateStatus.rejected
    await db.commit()
    return _nda_to_dict(nda)


# ---------------------------------------------------------------------------
# B6 Schritt 5 -- Zugangsschranke fuer vertrauliche Unterlagen
# ---------------------------------------------------------------------------

async def require_nda_approved(tenant_id, candidate_id, db: AsyncSession) -> Optional[NDA]:
    """Guard-Funktion: wirft HTTPException(403), solange die NDA des
    Kandidaten nicht status==approved hat. Ueberall dort zu verwenden, wo
    vertrauliche Unterlagen sonst freigegeben wuerden (hier demonstriert
    durch den /confidential-documents Endpunkt unten).

    Ausnahme (nutzerseitig gefordert: 'ob ein NDA abgeschlossen werden
    sollte, muss der Agent abwaegen'): Wenn fuer diesen Kandidaten explizit
    per assess_nda_necessity() needs_nda=false eingeschaetzt wurde (siehe
    SupplierCandidate.nda_assessment_json) UND noch gar keine NDA-Zeile
    existiert, gilt das als legitimer 'kein NDA noetig'-Fall und der Zugriff
    wird erlaubt (gibt dann None statt einer NDA zurueck). Sobald irgendeine
    NDA-Zeile fuer den Kandidaten existiert (z.B. weil doch manuell
    entschieden wurde, eine zu entwerfen), gilt wieder die strikte Regel:
    dann MUSS sie approved sein."""
    r = await db.execute(select(NDA).where(NDA.supplier_candidate_id == candidate_id, NDA.tenant_id == tenant_id))
    nda = r.scalar_one_or_none()
    if nda:
        if nda.status != NDAStatus.approved:
            raise HTTPException(403, "Zugriff verweigert: Es liegt keine freigegebene NDA fuer diesen Kandidaten vor "
                                       "(status muss 'approved' sein, menschlich per /nda/{id}/approve gesetzt).")
        return nda

    cand_r = await db.execute(select(SupplierCandidate).where(SupplierCandidate.id == candidate_id, SupplierCandidate.tenant_id == tenant_id))
    cand = cand_r.scalar_one_or_none()
    assessment = (cand.nda_assessment_json if cand else None) or {}
    if assessment.get("needs_nda") is False:
        return None

    raise HTTPException(403, "Zugriff verweigert: Es liegt keine freigegebene NDA fuer diesen Kandidaten vor "
                               "(status muss 'approved' sein, menschlich per /nda/{id}/approve gesetzt).")


@router.get("/candidates/{candidate_id}/confidential-documents")
async def get_confidential_documents(candidate_id: str, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """Demonstriert die reale Durchsetzung von B6 Schritt 5: vertrauliche
    Unterlagen (hier: die Muss-Kriterien im Volltext + public_teaser der
    SourcingRequest als Platzhalter fuer 'interne Zeichnungen/Spezifikationen')
    werden erst nach bestaetigter NDA-Freigabe herausgegeben."""
    cand = await _get_candidate_or_404(candidate_id, membership.tenant_id, db)
    await require_nda_approved(membership.tenant_id, cand.id, db)
    req = (await db.execute(select(SourcingRequest).where(SourcingRequest.id == cand.sourcing_request_id))).scalar_one()
    return {
        "access_granted": True,
        "must_criteria_full": req.must_criteria_json,
        "confidential_notice": req.confidential_notice,
    }


# ---------------------------------------------------------------------------
# Self-Service Stammdaten-Eingabe (oeffentlich, token-basiert, kein Login)
# ---------------------------------------------------------------------------
# Kein authentifizierter Request -> kein app.tenant_id/app.user_id aus einer
# Session verfuegbar, daher get_admin_db (siehe Docstring dort). Die
# Tenant-Eingrenzung erfolgt stattdessen explizit ueber den per Token
# gefundenen Invite-/Kandidaten-Datensatz. Bankdaten werden hier bewusst
# NICHT abgefragt (B5-Entscheidung: Bankdaten laufen ausschliesslich ueber
# die eigenen /bank-data-Endpunkte mit separater Verifikation, nie per
# gewoehnlichem Formular/Mail-Link).

ONBOARDING_FIELD_LABELS = {
    "firmenname": "Firmenname",
    "rechtsform": "Rechtsform",
    "anschrift": "Anschrift",
    "ansprechpartner_angebot": "Ansprechpartner fuer das Angebot",
    "unterzeichnungsberechtigt_vertrag": "Unterschriftsberechtigte Person fuer Vertraege",
    "steuernummer": "Steuernummer",
    "ust_id": "USt-IdNr.",
    "handelsregisternummer": "Handelsregisternummer",
    "mitarbeiterzahl": "Mitarbeiterzahl",
}


class OnboardingSubmitPayload(BaseModel):
    token: str
    fields: dict  # {field_name: value}
    confirmed_accurate: bool
    confirmed_by_name: str


async def _get_invite_and_candidate_by_token(token: str, db: AsyncSession):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    r = await db.execute(select(CandidateOnboardingInvite).where(CandidateOnboardingInvite.token_hash == token_hash))
    invite = r.scalar_one_or_none()
    if not invite:
        raise HTTPException(404, "Dieser Link ist ungueltig.")
    if invite.status == CandidateOnboardingInviteStatus.completed:
        raise HTTPException(400, "Dieser Link wurde bereits verwendet.")
    cand_r = await db.execute(select(SupplierCandidate).where(SupplierCandidate.id == invite.supplier_candidate_id))
    cand = cand_r.scalar_one_or_none()
    if not cand:
        raise HTTPException(404, "Zugehoeriger Kandidat nicht gefunden.")
    return invite, cand


@router.get("/onboarding/status")
async def onboarding_status(token: str, db: AsyncSession = Depends(get_admin_db)):
    _invite, cand = await _get_invite_and_candidate_by_token(token, db)
    sb = cand.stammblatt_json or {}
    return {
        "company_name": cand.company_name,
        "contact_name": cand.contact_name,
        "fields": {
            k: {"label": v, "current_value": (sb.get(k) or {}).get("value")}
            for k, v in ONBOARDING_FIELD_LABELS.items()
        },
    }


@router.post("/onboarding/submit")
async def onboarding_submit(payload: OnboardingSubmitPayload, db: AsyncSession = Depends(get_admin_db)):
    if not payload.confirmed_accurate:
        raise HTTPException(400, "Bitte bestaetigen Sie die Richtigkeit der Angaben.")
    if not payload.confirmed_by_name.strip():
        raise HTTPException(400, "Bitte Ihren Namen zur Bestaetigung angeben.")

    invite, cand = await _get_invite_and_candidate_by_token(payload.token, db)

    sb = dict(cand.stammblatt_json or {})
    now_iso = datetime.utcnow().isoformat()
    for field_name, value in payload.fields.items():
        if field_name not in ONBOARDING_FIELD_LABELS:
            continue
        sb[field_name] = {
            "value": (str(value).strip() or None) if value is not None else None,
            "status": "eingegangen", "source": "self_service_portal", "reviewer": None,
            "updated_at": now_iso,
        }
    cand.stammblatt_json = sb

    invite.status = CandidateOnboardingInviteStatus.completed
    invite.confirmed_by_name = payload.confirmed_by_name
    invite.confirmed_at = datetime.utcnow()
    await db.commit()

    return {"message": "Vielen Dank! Ihre Angaben wurden uebermittelt und werden nun geprueft."}
