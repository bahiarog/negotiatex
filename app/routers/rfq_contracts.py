"""
Teil B7-B9 des Agenten-Playbooks: Angebotsanfrage (RFQ), Angebotsvergleich/
-verhandlung und Vertragsentwurf bis Signatur-Bereitschaft.

Zwei Router in einer Datei (Judgment Call, siehe Bericht: RFQ und Vertrag
teilen Helfer/Hash-Muster und sind im selben Lebenszyklus verbunden -- Trennung
in zwei Dateien haette v.a. Boilerplate verdoppelt): `rfq_router` wird unter
/api/v1/rfq, `contracts_router` unter /api/v1/contracts eingehaengt (siehe
main.py).

Sicherheitsprinzipien (identisch zu routers/negotiation.py und
routers/sourcing.py):
  - Jeder Anthropic-Call (services/rfq_classifier.py) bekommt KEIN
    `tools`-Argument.
  - Jeder Versand (RFQ-Mail, Vertrags-Mail) ist hash-gebunden freigegeben:
    Hash bei Freigabe gespeichert, unmittelbar vor dem tatsaechlichen Versand
    aus dem AKTUELLEN Inhalt neu berechnet und verglichen -- jede Aenderung
    nach Freigabe blockiert den Versand (400).
  - B8 verhandelt NICHT selbst -- `/rfq/{id}/award` erzeugt einen `Case` +
    eine `NegotiationStrategy` und uebergibt an die BESTEHENDEN Teil-A-
    Endpunkte (routers/negotiation.py). Es gibt hier keine zweite
    Preisverhandlungs-Engine.
  - B9: kein Code-Pfad ausser `human_confirmed_signed` (unten) kann
    Contract.status/human_confirmed_signed je auf "signiert" setzen. Eine neue
    Vertragsversion (`/new-version`) hebt jede Freigabe der alten Version
    technisch auf (`_ensure_contract_not_superseded`), nicht nur dokumentiert.
"""
import hashlib
import logging
import re
import uuid
from datetime import datetime
from decimal import Decimal
from email.utils import make_msgid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from deps import get_current_user, get_current_membership
from models_v2 import Case, CaseStatus, CaseEvent
from models_sourcing import SourcingRequest, SupplierCandidate, CandidateStatus, NDA, NDAStatus
from models_negotiation import NegotiationStrategy
from models_contracts import (
    RFQ, RFQStatus, RFQInvitation, RFQAction, RFQActionStatus, RFQApproval,
    RFQOffer, OfferStatus, ComparabilityFlag,
    ContractTemplate, Contract, ContractStatus,
    ContractAction, ContractActionStatus, ContractApproval, ContractEvent,
)
from routers.cases import _transition as _case_transition
from services.email_sender import send_negotiation_email
from services.rfq_classifier import extract_offer_fields, compute_offer_comparison
from services.contract_diff import detect_redline, classify_clause_changes

logger = logging.getLogger(__name__)
rfq_router = APIRouter()
contracts_router = APIRouter()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _compute_hash(*parts) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def require_nda_approved(tenant_id, candidate_id, db: AsyncSession) -> NDA:
    """Identische Guard-Logik wie routers/sourcing.require_nda_approved
    (hier dupliziert statt importiert, um den bestehenden Sourcing-Router
    unangetastet zu lassen -- siehe Bericht). Eine RFQ mit echter Spezifikation
    darf NIE an einen Kandidaten ohne freigegebene NDA gehen."""
    r = await db.execute(select(NDA).where(NDA.supplier_candidate_id == candidate_id, NDA.tenant_id == tenant_id))
    nda = r.scalar_one_or_none()
    if not nda or nda.status != NDAStatus.approved:
        raise HTTPException(403, "Zugriff verweigert: Fuer diesen Kandidaten liegt keine freigegebene NDA vor "
                                   "(status muss 'approved' sein, menschlich per /sourcing/nda/{id}/approve gesetzt). "
                                   "Eine RFQ mit echter Spezifikation darf nur an NDA-freigegebene Kandidaten gehen.")
    return nda


async def _get_rfq_or_404(rfq_id: str, tenant_id, db: AsyncSession) -> RFQ:
    try:
        rid = uuid.UUID(rfq_id)
    except ValueError:
        raise HTTPException(404, "RFQ nicht gefunden.")
    r = await db.execute(select(RFQ).where(RFQ.id == rid))
    rfq = r.scalar_one_or_none()
    if not rfq or rfq.tenant_id != tenant_id:
        raise HTTPException(404, "RFQ nicht gefunden.")
    return rfq


async def _get_candidate_or_404(candidate_id, tenant_id, db: AsyncSession) -> SupplierCandidate:
    try:
        cid = candidate_id if isinstance(candidate_id, uuid.UUID) else uuid.UUID(str(candidate_id))
    except ValueError:
        raise HTTPException(404, "Kandidat nicht gefunden.")
    r = await db.execute(select(SupplierCandidate).where(SupplierCandidate.id == cid))
    cand = r.scalar_one_or_none()
    if not cand or cand.tenant_id != tenant_id:
        raise HTTPException(404, "Kandidat nicht gefunden.")
    return cand


async def _get_rfq_action_or_404(action_id: str, rfq_id, db: AsyncSession) -> RFQAction:
    try:
        aid = uuid.UUID(action_id)
    except ValueError:
        raise HTTPException(404, "Aktion nicht gefunden.")
    r = await db.execute(select(RFQAction).where(RFQAction.id == aid))
    action = r.scalar_one_or_none()
    if not action or action.rfq_id != rfq_id:
        raise HTTPException(404, "Aktion nicht gefunden.")
    return action


async def _get_offer_or_404(offer_id: str, tenant_id, db: AsyncSession) -> RFQOffer:
    try:
        oid = uuid.UUID(offer_id)
    except ValueError:
        raise HTTPException(404, "Angebot nicht gefunden.")
    r = await db.execute(select(RFQOffer).where(RFQOffer.id == oid))
    offer = r.scalar_one_or_none()
    if not offer or offer.tenant_id != tenant_id:
        raise HTTPException(404, "Angebot nicht gefunden.")
    return offer


async def _check_cross_bidder_confidentiality(db: AsyncSession, rfq_id, tenant_id, recipient_candidate_id, body_text: str) -> list[str]:
    """B7 Vertraulichkeitsregel, als Code-Block statt nur UI-Hinweis: scannt
    den fuer Bieter X bestimmten Text darauf, ob er die gespeicherten
    Angebotszahlen ODER den Firmennamen eines ANDEREN Bieters in dieser RFQ
    enthaelt. Best-effort-Textscan (gleiches Prinzip wie die
    _FORBIDDEN_PHRASES-Pruefung in routers/negotiation.py) -- KEINE formale
    Garantie, dass jede indirekte Erwaehnung erkannt wird, aber eine echte
    Code-Schranke, kein reiner Dokumentations-Hinweis."""
    problems: list[str] = []
    r = await db.execute(
        select(RFQOffer, SupplierCandidate)
        .join(SupplierCandidate, SupplierCandidate.id == RFQOffer.supplier_candidate_id)
        .where(RFQOffer.rfq_id == rfq_id, RFQOffer.tenant_id == tenant_id)
    )
    low_body = (body_text or "").lower()
    seen_candidates = set()
    for offer, cand in r.all():
        if str(cand.id) == str(recipient_candidate_id):
            continue
        if cand.company_name and cand.company_name.lower() in low_body and str(cand.id) not in seen_candidates:
            problems.append(f"Enthaelt den Firmennamen eines anderen Bieters ('{cand.company_name}').")
            seen_candidates.add(str(cand.id))
        for label, value in (("Stueckpreis", offer.unit_price), ("Fracht", offer.freight_cost), ("weitere Kosten", offer.other_costs)):
            if value is None:
                continue
            variants = {str(value), f"{Decimal(value):.2f}", f"{Decimal(value):,.2f}".replace(",", ".")}
            if any(v in body_text for v in variants if v):
                problems.append(f"Enthaelt eine Zahl ({label}: {value}), die dem gespeicherten Angebot eines anderen Bieters ('{cand.company_name}') entspricht.")
    return problems


# ---------------------------------------------------------------------------
# B7 -- RFQ Schemas
# ---------------------------------------------------------------------------

class RFQCreate(BaseModel):
    sourcing_request_id: str
    spec_text: str
    expected_quantity: Optional[Decimal] = None
    expected_unit: Optional[str] = None
    currency: str = "EUR"
    deadline: datetime
    test_mode: bool = True


RFQ_INVITE_SUBJECT_TMPL = "{prefix}Angebotsanfrage {vorgang}"
RFQ_INVITE_BODY_TMPL = (
    "Guten Tag,\n\n"
    "bitte reichen Sie bis zum {frist} Ihr Angebot fuer folgenden Bedarf gemaess Spezifikation ein:\n\n"
    "{bedarf}\n\n"
    "Spezifikation:\n{spec}\n\n"
    "Bitte nennen Sie: Stueckpreis netto, Gesamtwarenwert, Fracht, weitere Kosten, Liefertermin, "
    "Zahlungsbedingungen und Angebotsguelt igkeit.\n"
    "Bestaetigen Sie die Spezifikation ausdruecklich und kennzeichnen Sie jede Abweichung.\n\n"
    "Diese Anfrage begruendet keine Bestellung.\n\n"
    "Hinweis zur Vertraulichkeit: Eine relevante Klarstellung der Anforderungen wird nach Freigabe allen "
    "betroffenen Bietern zur Verfuegung gestellt. Individuelle Preise oder vertrauliche Konditionen eines "
    "Mitbewerbers werden nicht weitergegeben.\n\n"
    "Freundliche Gruesse,\nNegotiateX – KI-gestuetzte Beschaffungsassistenz.\n"
    "{testnote}"
).replace("Angebotsguelt igkeit", "Angebotsgueltigkeit")


def _rfq_to_dict(r: RFQ) -> dict:
    return {
        "id": str(r.id), "sourcing_request_id": str(r.sourcing_request_id),
        "spec_version": r.spec_version, "spec_text": r.spec_text,
        "expected_quantity": str(r.expected_quantity) if r.expected_quantity is not None else None,
        "expected_unit": r.expected_unit, "currency": r.currency,
        "deadline": r.deadline, "test_mode": r.test_mode,
        "status": r.status.value if hasattr(r.status, "value") else r.status,
        "awarded_offer_id": str(r.awarded_offer_id) if r.awarded_offer_id else None,
        "awarded_case_id": str(r.awarded_case_id) if r.awarded_case_id else None,
        "created_at": r.created_at,
    }


def _rfq_action_to_dict(a: RFQAction) -> dict:
    return {
        "id": str(a.id), "rfq_id": str(a.rfq_id), "supplier_candidate_id": str(a.supplier_candidate_id),
        "kind": a.kind, "recipient_email": a.recipient_email,
        "rendered_subject": a.rendered_subject, "rendered_body": a.rendered_body,
        "payload_hash": a.payload_hash, "status": a.status.value if hasattr(a.status, "value") else a.status,
        "created_at": a.created_at,
    }


# ---------------------------------------------------------------------------
# B7 -- Endpoints
# ---------------------------------------------------------------------------

@rfq_router.post("")
async def create_rfq(payload: RFQCreate, user=Depends(get_current_user), membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    try:
        srid = uuid.UUID(payload.sourcing_request_id)
    except ValueError:
        raise HTTPException(404, "Suchauftrag nicht gefunden.")
    r = await db.execute(select(SourcingRequest).where(SourcingRequest.id == srid))
    req = r.scalar_one_or_none()
    if not req or req.tenant_id != membership.tenant_id:
        raise HTTPException(404, "Suchauftrag nicht gefunden.")

    rfq = RFQ(
        tenant_id=membership.tenant_id, sourcing_request_id=req.id, spec_text=payload.spec_text,
        expected_quantity=payload.expected_quantity, expected_unit=payload.expected_unit,
        currency=payload.currency, deadline=payload.deadline, test_mode=payload.test_mode,
        created_by=str(user.id),
    )
    db.add(rfq)
    await db.commit()
    await db.refresh(rfq)
    return _rfq_to_dict(rfq)


@rfq_router.get("/{rfq_id}")
async def get_rfq(rfq_id: str, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    rfq = await _get_rfq_or_404(rfq_id, membership.tenant_id, db)
    return _rfq_to_dict(rfq)


class InvitePayload(BaseModel):
    supplier_candidate_id: str


@rfq_router.post("/{rfq_id}/invite")
async def invite_candidate(rfq_id: str, payload: InvitePayload, user=Depends(get_current_user),
                            membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """B7: Einladung mit echter Spezifikation -- NDA-Gate zuerst (require_nda_approved),
    dann Entwurf (noch nicht gesendet, siehe /actions/{id}/approve)."""
    rfq = await _get_rfq_or_404(rfq_id, membership.tenant_id, db)
    cand = await _get_candidate_or_404(payload.supplier_candidate_id, membership.tenant_id, db)
    await require_nda_approved(membership.tenant_id, cand.id, db)
    if not cand.contact_email:
        raise HTTPException(400, "Kandidat hat keine hinterlegte Kontakt-E-Mail.")

    existing = await db.execute(select(RFQInvitation).where(RFQInvitation.rfq_id == rfq.id, RFQInvitation.supplier_candidate_id == cand.id))
    if not existing.scalar_one_or_none():
        db.add(RFQInvitation(tenant_id=membership.tenant_id, rfq_id=rfq.id, supplier_candidate_id=cand.id))

    req = (await db.execute(select(SourcingRequest).where(SourcingRequest.id == rfq.sourcing_request_id))).scalar_one()
    vorgang = str(rfq.id)[:8]
    subject = RFQ_INVITE_SUBJECT_TMPL.format(prefix="[TEST – ]" if rfq.test_mode else "", vorgang=vorgang)
    body = RFQ_INVITE_BODY_TMPL.format(
        frist=rfq.deadline.strftime("%d.%m.%Y"), bedarf=req.bedarf_text, spec=rfq.spec_text,
        testnote="Testlauf, keine Beauftragung." if rfq.test_mode else "",
    )

    guard_problems = await _check_cross_bidder_confidentiality(db, rfq.id, membership.tenant_id, cand.id, body)
    if guard_problems:
        raise HTTPException(400, "Vertraulichkeits-Guard blockiert Entwurf: " + "; ".join(guard_problems))

    action = RFQAction(
        tenant_id=membership.tenant_id, rfq_id=rfq.id, supplier_candidate_id=cand.id, kind="invite",
        recipient_email=cand.contact_email, rendered_subject=subject, rendered_body=body,
        status=RFQActionStatus.draft, created_by=str(user.id),
    )
    db.add(action)
    await db.commit()
    await db.refresh(action)
    return _rfq_action_to_dict(action)


class ComposePayload(BaseModel):
    kind: str = "clarification_broadcast"
    subject: str
    body: str


@rfq_router.post("/{rfq_id}/candidates/{candidate_id}/compose")
async def compose_rfq_message(rfq_id: str, candidate_id: str, payload: ComposePayload, user=Depends(get_current_user),
                               membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """Freiform-Entwurf an EINEN Bieter (z.B. eine freigegebene Klarstellung,
    oder eine Rueckfragen-Antwort) -- durchlaeuft denselben
    Vertraulichkeits-Guard WIE bei der Einladung, und zwar schon beim Entwurf
    (nicht erst beim Versand), damit ein Versuch, Daten eines anderen Bieters
    weiterzugeben, sofort sichtbar blockiert wird."""
    rfq = await _get_rfq_or_404(rfq_id, membership.tenant_id, db)
    cand = await _get_candidate_or_404(candidate_id, membership.tenant_id, db)
    if not cand.contact_email:
        raise HTTPException(400, "Kandidat hat keine hinterlegte Kontakt-E-Mail.")

    guard_problems = await _check_cross_bidder_confidentiality(db, rfq.id, membership.tenant_id, cand.id, payload.body)
    if guard_problems:
        raise HTTPException(400, "Vertraulichkeits-Guard blockiert Entwurf (Angebotsdaten eines anderen Bieters erkannt): "
                                   + "; ".join(guard_problems))

    action = RFQAction(
        tenant_id=membership.tenant_id, rfq_id=rfq.id, supplier_candidate_id=cand.id, kind=payload.kind,
        recipient_email=cand.contact_email, rendered_subject=payload.subject, rendered_body=payload.body,
        status=RFQActionStatus.draft, created_by=str(user.id),
    )
    db.add(action)
    await db.commit()
    await db.refresh(action)
    return _rfq_action_to_dict(action)


@rfq_router.get("/{rfq_id}/actions/{action_id}")
async def get_rfq_action(rfq_id: str, action_id: str, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    rfq = await _get_rfq_or_404(rfq_id, membership.tenant_id, db)
    action = await _get_rfq_action_or_404(action_id, rfq.id, db)
    return _rfq_action_to_dict(action)


@rfq_router.post("/{rfq_id}/actions/{action_id}/approve")
async def approve_rfq_action(rfq_id: str, action_id: str, user=Depends(get_current_user),
                              membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    rfq = await _get_rfq_or_404(rfq_id, membership.tenant_id, db)
    action = await _get_rfq_action_or_404(action_id, rfq.id, db)

    # Guard erneut unmittelbar vor Versand pruefen (Verteidigung in der Tiefe --
    # der Inhalt koennte sich theoretisch zwischen Entwurf und Freigabe nicht
    # mehr aendern, da es keinen Edit-Endpunkt gibt, aber die Pruefung ist billig).
    guard_problems = await _check_cross_bidder_confidentiality(db, rfq.id, membership.tenant_id, action.supplier_candidate_id, action.rendered_body or "")
    if guard_problems:
        raise HTTPException(400, "Vertraulichkeits-Guard blockiert Versand: " + "; ".join(guard_problems))

    if action.status == RFQActionStatus.draft:
        h = _compute_hash(action.recipient_email, action.rendered_subject, action.rendered_body)
        action.payload_hash = h
        action.status = RFQActionStatus.approved
        db.add(RFQApproval(action_id=action.id, approved_by=str(user.id), payload_hash=h))
        await db.commit()
    elif action.status != RFQActionStatus.approved:
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
    action.status = RFQActionStatus.sent

    if action.kind == "invite":
        inv = (await db.execute(select(RFQInvitation).where(
            RFQInvitation.rfq_id == rfq.id, RFQInvitation.supplier_candidate_id == action.supplier_candidate_id
        ))).scalar_one_or_none()
        if inv:
            inv.sent_at = datetime.utcnow()
            inv.message_id = msg_id
        if rfq.status == RFQStatus.draft:
            rfq.status = RFQStatus.sent

    await db.commit()
    return {"sent": True, "message_id": msg_id, "rfq_status": rfq.status.value, "payload_hash": action.payload_hash}


@rfq_router.post("/{rfq_id}/actions/{action_id}/reject")
async def reject_rfq_action(rfq_id: str, action_id: str, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    rfq = await _get_rfq_or_404(rfq_id, membership.tenant_id, db)
    action = await _get_rfq_action_or_404(action_id, rfq.id, db)
    if action.status not in (RFQActionStatus.draft, RFQActionStatus.approved):
        raise HTTPException(400, f"Aktion im Status '{action.status.value}' kann nicht abgelehnt werden.")
    action.status = RFQActionStatus.rejected
    await db.commit()
    return {"status": "rejected"}


# ---------------------------------------------------------------------------
# Offers (manuelle Erfassung oder Upload+KI-Extraktion)
# ---------------------------------------------------------------------------

class OfferCreate(BaseModel):
    supplier_candidate_id: str
    unit_price: Optional[Decimal] = None
    quantity: Optional[Decimal] = None
    freight_cost: Optional[Decimal] = Decimal("0")
    other_costs: Optional[Decimal] = Decimal("0")
    currency: str = "EUR"
    delivery_date: Optional[str] = None
    payment_terms: Optional[str] = None
    offer_validity_until: Optional[datetime] = None
    scope_note: Optional[str] = None
    spec_confirmed: Optional[bool] = None
    raw_extracted_json: Optional[dict] = None


def _offer_to_dict(o: RFQOffer) -> dict:
    return {
        "id": str(o.id), "rfq_id": str(o.rfq_id), "supplier_candidate_id": str(o.supplier_candidate_id),
        "version": o.version, "status": o.status.value if hasattr(o.status, "value") else o.status,
        "unit_price": str(o.unit_price) if o.unit_price is not None else None,
        "quantity": str(o.quantity) if o.quantity is not None else None,
        "freight_cost": str(o.freight_cost) if o.freight_cost is not None else None,
        "other_costs": str(o.other_costs) if o.other_costs is not None else None,
        "currency": o.currency, "delivery_date": o.delivery_date, "payment_terms": o.payment_terms,
        "offer_validity_until": o.offer_validity_until, "scope_note": o.scope_note,
        "spec_confirmed": o.spec_confirmed, "comparability_flag": o.comparability_flag.value if hasattr(o.comparability_flag, "value") else o.comparability_flag,
        "comparability_note": o.comparability_note, "received_at": o.received_at,
    }


async def _apply_comparability(offer: RFQOffer, rfq: RFQ):
    from services.rfq_classifier import _comparability
    flag, note = _comparability(offer.quantity, rfq.expected_quantity, offer.scope_note, offer.spec_confirmed)
    offer.comparability_flag = ComparabilityFlag(flag)
    offer.comparability_note = note


@rfq_router.post("/{rfq_id}/offers")
async def submit_offer(rfq_id: str, payload: OfferCreate, user=Depends(get_current_user),
                        membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    rfq = await _get_rfq_or_404(rfq_id, membership.tenant_id, db)
    cand = await _get_candidate_or_404(payload.supplier_candidate_id, membership.tenant_id, db)

    inv = (await db.execute(select(RFQInvitation).where(
        RFQInvitation.rfq_id == rfq.id, RFQInvitation.supplier_candidate_id == cand.id
    ))).scalar_one_or_none()
    if not inv or not inv.sent_at:
        raise HTTPException(400, "Kandidat wurde fuer diese RFQ nicht (nachweislich per Versand) eingeladen -- kein Angebot erfassbar.")

    offer = RFQOffer(
        tenant_id=membership.tenant_id, rfq_id=rfq.id, supplier_candidate_id=cand.id, version=1,
        unit_price=payload.unit_price, quantity=payload.quantity, freight_cost=payload.freight_cost or Decimal("0"),
        other_costs=payload.other_costs or Decimal("0"), currency=payload.currency, delivery_date=payload.delivery_date,
        payment_terms=payload.payment_terms, offer_validity_until=payload.offer_validity_until,
        scope_note=payload.scope_note, spec_confirmed=payload.spec_confirmed,
        raw_extracted_json=payload.raw_extracted_json, created_by=str(user.id),
    )
    await _apply_comparability(offer, rfq)
    db.add(offer)
    if rfq.status in (RFQStatus.sent,):
        rfq.status = RFQStatus.collecting_offers
    await db.flush()

    # B7: Eingangsbestaetigung an den Bieter -- als Entwurf, noch nicht gesendet.
    confirm_body = (
        f"Guten Tag,\n\nwir bestaetigen den Eingang Ihres Angebots fuer Vorgang {str(rfq.id)[:8]}.\n"
        f"Die Pruefung erfolgt im Rahmen der laufenden Angebotsfrist.\n\nFreundliche Gruesse,\nNegotiateX."
    )
    db.add(RFQAction(
        tenant_id=membership.tenant_id, rfq_id=rfq.id, supplier_candidate_id=cand.id, kind="receipt_confirmation",
        recipient_email=cand.contact_email or "unbekannt@example.invalid",
        rendered_subject=f"Eingangsbestaetigung Angebot {str(rfq.id)[:8]}", rendered_body=confirm_body,
        status=RFQActionStatus.draft, created_by=str(user.id),
    ))
    await db.commit()
    await db.refresh(offer)
    return _offer_to_dict(offer)


@rfq_router.post("/{rfq_id}/offers/upload")
async def upload_offer(rfq_id: str, supplier_candidate_id: str = Form(...), file: UploadFile = File(...),
                        user=Depends(get_current_user), membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """B7 Hintergrundarbeit: Angebotstext/-PDF gegen erwartete strukturierte
    Felder parsen. Fehlende Felder bleiben unknown/null -- nie geraten (siehe
    services/rfq_classifier.extract_offer_fields, identisches Prinzip wie
    suppliers.py EXTRACTION_SYSTEM_PROMPT)."""
    rfq = await _get_rfq_or_404(rfq_id, membership.tenant_id, db)
    cand = await _get_candidate_or_404(supplier_candidate_id, membership.tenant_id, db)

    inv = (await db.execute(select(RFQInvitation).where(
        RFQInvitation.rfq_id == rfq.id, RFQInvitation.supplier_candidate_id == cand.id
    ))).scalar_one_or_none()
    if not inv or not inv.sent_at:
        raise HTTPException(400, "Kandidat wurde fuer diese RFQ nicht eingeladen -- kein Angebot erfassbar.")

    content = await file.read()
    if len(content) > 15 * 1024 * 1024:
        raise HTTPException(400, "Datei zu gross (max. 15 MB).")
    import tempfile, os as _os
    from pathlib import Path
    suffix = Path(file.filename or "").suffix.lower() or ".pdf"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        from services.pdf_parser import extract_text
        text = await extract_text(tmp_path, file.filename or "offer.pdf")
    finally:
        _os.unlink(tmp_path)
    if not text or text.startswith("[Error") or text.startswith("[Unsupported"):
        raise HTTPException(400, f"Datei konnte nicht gelesen werden: {text}")

    extracted = extract_offer_fields(text)
    offer = RFQOffer(
        tenant_id=membership.tenant_id, rfq_id=rfq.id, supplier_candidate_id=cand.id, version=1,
        unit_price=extracted.get("unit_price"), quantity=extracted.get("quantity"),
        freight_cost=extracted.get("freight_cost") or Decimal("0"), other_costs=extracted.get("other_costs") or Decimal("0"),
        currency=extracted.get("currency") or "EUR", delivery_date=extracted.get("delivery_date"),
        payment_terms=extracted.get("payment_terms"), scope_note=extracted.get("scope_note"),
        spec_confirmed=extracted.get("spec_confirmed"), raw_extracted_json=extracted, created_by=str(user.id),
    )
    await _apply_comparability(offer, rfq)
    db.add(offer)
    if rfq.status == RFQStatus.sent:
        rfq.status = RFQStatus.collecting_offers
    await db.commit()
    await db.refresh(offer)
    result = _offer_to_dict(offer)
    result["extraction_note"] = "Fehlende Felder wurden NICHT geraten -- bitte manuell pruefen/ergaenzen."
    return result


class NegotiatedOfferUpdate(BaseModel):
    unit_price: Decimal
    freight_cost: Decimal = Decimal("0")
    other_costs: Optional[Decimal] = None
    note: Optional[str] = None


@rfq_router.post("/{rfq_id}/offers/{offer_id}/record-negotiated-offer")
async def record_negotiated_offer(rfq_id: str, offer_id: str, payload: NegotiatedOfferUpdate, user=Depends(get_current_user),
                                   membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """B8: haelt das ERGEBNIS einer ueber die bestehende Teil-A-Engine
    (routers/negotiation.py) gefuehrten Preisverhandlung als NEUE RFQOffer-Version
    fest -- die Verhandlung selbst laeuft ueber die Teil-A-Endpunkte
    (NegotiationStrategy/NegotiationAction), hier wird nur das Ergebnis
    nachgetragen, damit der Vergleich (GET /comparison) 'Anbieter A nach
    Verhandlung' als eigene, nachvollziehbare Zeile zeigen kann."""
    rfq = await _get_rfq_or_404(rfq_id, membership.tenant_id, db)
    original = await _get_offer_or_404(offer_id, membership.tenant_id, db)
    if original.rfq_id != rfq.id:
        raise HTTPException(404, "Angebot gehoert nicht zu dieser RFQ.")

    new_offer = RFQOffer(
        tenant_id=membership.tenant_id, rfq_id=rfq.id, supplier_candidate_id=original.supplier_candidate_id,
        version=original.version + 1, unit_price=payload.unit_price, quantity=original.quantity,
        freight_cost=payload.freight_cost, other_costs=payload.other_costs if payload.other_costs is not None else original.other_costs,
        currency=original.currency, delivery_date=original.delivery_date, payment_terms=original.payment_terms,
        offer_validity_until=original.offer_validity_until, scope_note=original.scope_note,
        spec_confirmed=original.spec_confirmed, comparability_flag=original.comparability_flag,
        comparability_note=(payload.note or original.comparability_note),
        created_by=str(user.id),
    )
    await _apply_comparability(new_offer, rfq)
    db.add(new_offer)
    await db.flush()
    original.status = OfferStatus.superseded
    original.superseded_by_id = new_offer.id
    await db.commit()
    await db.refresh(new_offer)
    return _offer_to_dict(new_offer)


@rfq_router.get("/{rfq_id}/comparison")
async def get_comparison(rfq_id: str, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    rfq = await _get_rfq_or_404(rfq_id, membership.tenant_id, db)
    req = (await db.execute(select(SourcingRequest).where(SourcingRequest.id == rfq.sourcing_request_id))).scalar_one()
    offers = (await db.execute(select(RFQOffer).where(RFQOffer.rfq_id == rfq.id))).scalars().all()
    weights = {"price": req.weight_price, "quality": req.weight_quality, "delivery": req.weight_delivery}
    result = compute_offer_comparison(rfq, list(offers), weights)
    if offers and rfq.status in (RFQStatus.collecting_offers, RFQStatus.sent):
        rfq.status = RFQStatus.comparison_ready
        await db.commit()
    return result


class AwardPayload(BaseModel):
    offer_id: str
    note: Optional[str] = None
    round1_discount_pct: Decimal = Decimal("1.0")   # z.B. 1% unter Angebotspreis als Runde-1-Ziel
    round2_discount_pct: Decimal = Decimal("2.0")


@rfq_router.post("/{rfq_id}/award")
async def award_rfq(rfq_id: str, payload: AwardPayload, user=Depends(get_current_user),
                     membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """B8 Zuschlag -- rein menschliche Aktion (expliziter Endpunkt-Aufruf,
    niemals automatisch). Erzeugt einen Case + eine NegotiationStrategy,
    vorbefuellt aus dem Vergleich, und uebergibt damit an die BESTEHENDE
    Teil-A-Verhandlungsengine (routers/negotiation.py) -- 'Einkauf entscheidet
    ueber Eignung und Zuschlag', der Preisvorschlag selbst laeuft danach ueber
    POST /api/v1/negotiation/cases/{case_id}/actions/draft wie gehabt."""
    rfq = await _get_rfq_or_404(rfq_id, membership.tenant_id, db)
    if rfq.status == RFQStatus.awarded:
        raise HTTPException(400, "RFQ wurde bereits vergeben.")
    offer = await _get_offer_or_404(payload.offer_id, membership.tenant_id, db)
    if offer.rfq_id != rfq.id:
        raise HTTPException(404, "Angebot gehoert nicht zu dieser RFQ.")
    if offer.unit_price is None:
        raise HTTPException(400, "Angebot hat keinen Stueckpreis -- kann nicht als Zuschlagsgrundlage dienen.")

    cand = await _get_candidate_or_404(offer.supplier_candidate_id, membership.tenant_id, db)
    req = (await db.execute(select(SourcingRequest).where(SourcingRequest.id == rfq.sourcing_request_id))).scalar_one()

    case = Case(tenant_id=membership.tenant_id, title=f"{req.title} — Verhandlung {cand.company_name}",
                category="rfq_negotiation", status=CaseStatus.RECEIVED, case_version=1)
    db.add(case)
    await db.flush()
    db.add(CaseEvent(case_id=case.id, tenant_id=case.tenant_id, from_status=None, to_status=CaseStatus.RECEIVED.value,
                      actor=str(user.id), reason=f"Aus RFQ-Zuschlag erstellt (Angebot {offer.id})."))

    start = Decimal(str(offer.unit_price))
    r1 = (start * (Decimal("100") - payload.round1_discount_pct) / Decimal("100")).quantize(Decimal("0.01"))
    r2 = (start * (Decimal("100") - payload.round2_discount_pct) / Decimal("100")).quantize(Decimal("0.01"))
    strategy = NegotiationStrategy(
        tenant_id=membership.tenant_id, case_id=case.id,
        starting_price_net=start, round1_price_net=r1, round2_price_net=r2, price_cap_net=start,
        currency=offer.currency or "EUR", scope_text=req.bedarf_text,
        usage_rights_text="Wie im Angebot/der RFQ-Spezifikation beschrieben, unveraendert.",
        delivery_date=offer.delivery_date or "wie angeboten", max_rounds=2,
        supplier_email=cand.contact_email or "unbekannt@example.invalid", created_by=str(user.id),
    )
    db.add(strategy)
    await _case_transition(db, case, CaseStatus.READY_TO_DRAFT, actor=str(user.id),
                            reason="Verhandlungsstrategie aus RFQ-Vergleich uebernommen (Zuschlagsentscheidung Einkauf).")

    rfq.status = RFQStatus.awarded
    rfq.awarded_offer_id = offer.id
    rfq.awarded_by = str(user.id)
    rfq.awarded_at = datetime.utcnow()
    rfq.awarded_case_id = case.id
    await db.commit()
    await db.refresh(strategy)

    return {
        "rfq_status": rfq.status.value, "case_id": str(case.id), "strategy_id": str(strategy.id),
        "einkauf_entscheidet_ueber_eignung_und_zuschlag": True,
        "note": "Zuschlag menschlich entschieden. Preisverhandlung laeuft ab jetzt ueber die bestehende "
                "Teil-A-Engine (POST /api/v1/negotiation/cases/{case_id}/actions/draft) -- keine zweite Engine.",
    }


# ---------------------------------------------------------------------------
# B9 -- Vertraege
# ---------------------------------------------------------------------------

async def _get_template_or_404(template_id: str, tenant_id, db: AsyncSession) -> ContractTemplate:
    try:
        tid = uuid.UUID(template_id)
    except ValueError:
        raise HTTPException(404, "Vorlage nicht gefunden.")
    r = await db.execute(select(ContractTemplate).where(ContractTemplate.id == tid))
    t = r.scalar_one_or_none()
    if not t or t.tenant_id != tenant_id:
        raise HTTPException(404, "Vorlage nicht gefunden.")
    return t


async def _get_contract_or_404(contract_id: str, tenant_id, db: AsyncSession) -> Contract:
    try:
        cid = uuid.UUID(contract_id)
    except ValueError:
        raise HTTPException(404, "Vertrag nicht gefunden.")
    r = await db.execute(select(Contract).where(Contract.id == cid))
    c = r.scalar_one_or_none()
    if not c or c.tenant_id != tenant_id:
        raise HTTPException(404, "Vertrag nicht gefunden.")
    return c


async def _get_contract_action_or_404(action_id: str, contract_id, db: AsyncSession) -> ContractAction:
    try:
        aid = uuid.UUID(action_id)
    except ValueError:
        raise HTTPException(404, "Aktion nicht gefunden.")
    r = await db.execute(select(ContractAction).where(ContractAction.id == aid))
    a = r.scalar_one_or_none()
    if not a or a.contract_id != contract_id:
        raise HTTPException(404, "Aktion nicht gefunden.")
    return a


async def _contract_transition(db: AsyncSession, contract: Contract, new_status: ContractStatus, actor: str, reason: str):
    old = contract.status
    old_value = old.value if hasattr(old, "value") else old
    contract.status = new_status
    contract.updated_at = datetime.utcnow()
    db.add(ContractEvent(contract_id=contract.id, tenant_id=contract.tenant_id,
                          from_status=old_value, to_status=new_status.value, actor=actor, reason=reason))


def _ensure_contract_not_superseded(contract: Contract):
    """Technische Durchsetzung von 'eine neue Vertragsversion hebt die
    bisherige Freigabe auf': sobald `superseded_by_version` gesetzt ist
    (durch /new-version auf der ALTEN Zeile), kann auf DIESER Vertragszeile
    keine Aktion mehr freigegeben/gesendet werden -- unabhaengig davon, ob die
    einzelne ContractAction selbst noch einen gueltigen Hash haette."""
    if contract.superseded_by_version is not None:
        raise HTTPException(
            400,
            f"Freigabe/Versand blockiert: Vertragsversion {contract.version} wurde durch Version "
            f"{contract.superseded_by_version} ersetzt. Eine neue Vertragsversion hebt jede Freigabe der "
            f"alten Version auf -- bitte mit der aktuellen Version fortfahren.",
        )


def _contract_to_dict(c: Contract) -> dict:
    return {
        "id": str(c.id), "lineage_id": str(c.lineage_id), "version": c.version,
        "superseded_by_version": c.superseded_by_version,
        "sourcing_request_id": str(c.sourcing_request_id) if c.sourcing_request_id else None,
        "case_id": str(c.case_id) if c.case_id else None,
        "rfq_id": str(c.rfq_id) if c.rfq_id else None,
        "offer_id": str(c.offer_id) if c.offer_id else None,
        "template_id": str(c.template_id) if c.template_id else None,
        "supplier_candidate_id": str(c.supplier_candidate_id) if c.supplier_candidate_id else None,
        "status": c.status.value if hasattr(c.status, "value") else c.status,
        "body_text": c.body_text, "payload_hash": c.payload_hash,
        "mandate_price_cap": str(c.mandate_price_cap) if c.mandate_price_cap is not None else None,
        "mandate_payment_terms_days_max": c.mandate_payment_terms_days_max,
        "mandate_delivery_date_latest": c.mandate_delivery_date_latest,
        "legal_review_required": c.legal_review_required, "commercial_review_required": c.commercial_review_required,
        "redline_detected": c.redline_detected, "redline_clause_categories": c.redline_clause_categories or [],
        "open_points_json": c.open_points_json or [],
        "human_confirmed_signed": c.human_confirmed_signed,
        "human_confirmed_signed_by": c.human_confirmed_signed_by, "human_confirmed_signed_at": c.human_confirmed_signed_at,
        "created_at": c.created_at, "updated_at": c.updated_at,
    }


# -- Templates --------------------------------------------------------------

class TemplateCreate(BaseModel):
    name: str
    body_text: str


@contracts_router.post("/templates")
async def create_template(payload: TemplateCreate, user=Depends(get_current_user),
                           membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """B9 Entwurf: eine NEUE Vorlage ist NIE automatisch freigegeben --
    template_approved_by_legal startet immer False."""
    t = ContractTemplate(tenant_id=membership.tenant_id, name=payload.name, body_text=payload.body_text,
                          template_approved_by_legal=False, created_by=str(user.id))
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return {"id": str(t.id), "name": t.name, "version": t.version, "template_approved_by_legal": t.template_approved_by_legal}


@contracts_router.get("/templates")
async def list_templates(membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(ContractTemplate).where(ContractTemplate.tenant_id == membership.tenant_id))
    return [{"id": str(t.id), "name": t.name, "version": t.version,
             "template_approved_by_legal": t.template_approved_by_legal} for t in r.scalars().all()]


@contracts_router.post("/templates/{template_id}/approve-by-legal")
async def approve_template_by_legal(template_id: str, user=Depends(get_current_user),
                                     membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """Einziger Code-Pfad, der template_approved_by_legal auf True setzen kann
    -- menschlich ausgeloest, kein automatisches Freischalten einer neuen Vorlage."""
    t = await _get_template_or_404(template_id, membership.tenant_id, db)
    t.template_approved_by_legal = True
    t.approved_by = str(user.id)
    t.approved_at = datetime.utcnow()
    await db.commit()
    return {"id": str(t.id), "template_approved_by_legal": True}


# -- Contract lifecycle -------------------------------------------------------

class ContractCreate(BaseModel):
    template_id: str
    body_text: str
    sourcing_request_id: Optional[str] = None
    case_id: Optional[str] = None
    rfq_id: Optional[str] = None
    offer_id: Optional[str] = None
    supplier_candidate_id: Optional[str] = None
    mandate_price_cap: Optional[Decimal] = None
    mandate_payment_terms_days_max: Optional[int] = None
    mandate_delivery_date_latest: Optional[str] = None


@contracts_router.post("")
async def create_contract(payload: ContractCreate, user=Depends(get_current_user),
                           membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """B9 Entwurf: fuellt eine Vorlage mit bestaetigten Konditionen (Preis,
    Liefertermin, Parteien) aus dem gewonnenen Angebot/der Verhandlung.
    Eine NICHT von Legal freigegebene Vorlage kann NICHT verwendet werden --
    harte 400, kein Soft-Warning."""
    template = await _get_template_or_404(payload.template_id, membership.tenant_id, db)
    if not template.template_approved_by_legal:
        raise HTTPException(400, "Vorlage ist nicht von Legal freigegeben (template_approved_by_legal=False) -- "
                                   "kann fuer keinen Vertragsentwurf verwendet werden.")

    def _uuid_or_none(v):
        try:
            return uuid.UUID(v) if v else None
        except ValueError:
            return None

    contract = Contract(
        tenant_id=membership.tenant_id, lineage_id=uuid.uuid4(), version=1,
        sourcing_request_id=_uuid_or_none(payload.sourcing_request_id), case_id=_uuid_or_none(payload.case_id),
        rfq_id=_uuid_or_none(payload.rfq_id), offer_id=_uuid_or_none(payload.offer_id),
        supplier_candidate_id=_uuid_or_none(payload.supplier_candidate_id), template_id=template.id,
        status=ContractStatus.draft, body_text=payload.body_text,
        mandate_price_cap=payload.mandate_price_cap, mandate_payment_terms_days_max=payload.mandate_payment_terms_days_max,
        mandate_delivery_date_latest=payload.mandate_delivery_date_latest, created_by=str(user.id),
    )
    db.add(contract)
    await db.flush()
    db.add(ContractEvent(contract_id=contract.id, tenant_id=contract.tenant_id, from_status=None,
                          to_status=ContractStatus.draft.value, actor=str(user.id), reason="Vertragsentwurf erstellt."))
    await db.commit()
    await db.refresh(contract)
    return _contract_to_dict(contract)


@contracts_router.get("/{contract_id}")
async def get_contract(contract_id: str, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    c = await _get_contract_or_404(contract_id, membership.tenant_id, db)
    return _contract_to_dict(c)


@contracts_router.get("/{contract_id}/events")
async def get_contract_events(contract_id: str, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    c = await _get_contract_or_404(contract_id, membership.tenant_id, db)
    r = await db.execute(select(ContractEvent).where(ContractEvent.contract_id == c.id).order_by(ContractEvent.created_at))
    return [{"from_status": e.from_status, "to_status": e.to_status, "actor": e.actor, "reason": e.reason,
             "created_at": e.created_at} for e in r.scalars().all()]


def _contract_action_to_dict(a: ContractAction) -> dict:
    return {
        "id": str(a.id), "contract_id": str(a.contract_id), "kind": a.kind,
        "clause_category": a.clause_category, "proposed_clause_text": a.proposed_clause_text,
        "recipient_email": a.recipient_email, "rendered_subject": a.rendered_subject, "rendered_body": a.rendered_body,
        "payload_hash": a.payload_hash, "status": a.status.value if hasattr(a.status, "value") else a.status,
        "contract_version_at_action": a.contract_version_at_action, "created_at": a.created_at,
    }


class ContractActionDraft(BaseModel):
    recipient_email: str
    subject: Optional[str] = None


@contracts_router.post("/{contract_id}/actions/draft")
async def draft_contract_send(contract_id: str, payload: ContractActionDraft, user=Depends(get_current_user),
                               membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """B9 Entwurf -> Versand: entwirft eine Aktion, die den AKTUELLEN
    Vertragstext an den Bieter sendet. An die aktuelle Version gebunden
    (`contract_version_at_action`)."""
    c = await _get_contract_or_404(contract_id, membership.tenant_id, db)
    _ensure_contract_not_superseded(c)
    if c.status not in (ContractStatus.draft, ContractStatus.commercial_review_required, ContractStatus.legal_review_required):
        raise HTTPException(400, f"Vertrag im Status '{c.status.value}' -- kein neuer Versand-Entwurf noetig/zulaessig.")

    subject = payload.subject or f"Vertragsentwurf (Version {c.version})"
    action = ContractAction(
        tenant_id=membership.tenant_id, contract_id=c.id, kind="send_draft",
        recipient_email=payload.recipient_email, rendered_subject=subject, rendered_body=c.body_text,
        status=ContractActionStatus.draft, contract_version_at_action=c.version, created_by=str(user.id),
    )
    db.add(action)
    await db.commit()
    await db.refresh(action)
    return _contract_action_to_dict(action)


class ClauseProposal(BaseModel):
    clause_category: str
    proposed_text: str
    recipient_email: str
    message_subject: str
    message_body: str


@contracts_router.post("/{contract_id}/propose-clause")
async def propose_clause(contract_id: str, payload: ClauseProposal, user=Depends(get_current_user),
                          membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """B9 Gegenvorschlag: Einfuegen einer freigegebenen Ersatzklausel UND das
    Verfassen der Begleitnachricht haengen BEIDE an dieser EINEN
    menschlichen Freigabe (siehe /actions/{id}/approve) -- vor der Freigabe
    wird nichts eingefuegt und nichts versendet."""
    c = await _get_contract_or_404(contract_id, membership.tenant_id, db)
    _ensure_contract_not_superseded(c)
    action = ContractAction(
        tenant_id=membership.tenant_id, contract_id=c.id, kind="propose_clause",
        clause_category=payload.clause_category, proposed_clause_text=payload.proposed_text,
        recipient_email=payload.recipient_email, rendered_subject=payload.message_subject,
        rendered_body=payload.message_body, status=ContractActionStatus.draft,
        contract_version_at_action=c.version, created_by=str(user.id),
    )
    db.add(action)
    await db.commit()
    await db.refresh(action)
    return _contract_action_to_dict(action)


@contracts_router.get("/{contract_id}/actions/{action_id}")
async def get_contract_action(contract_id: str, action_id: str, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    c = await _get_contract_or_404(contract_id, membership.tenant_id, db)
    a = await _get_contract_action_or_404(action_id, c.id, db)
    return _contract_action_to_dict(a)


@contracts_router.post("/{contract_id}/actions/{action_id}/approve")
async def approve_contract_action(contract_id: str, action_id: str, user=Depends(get_current_user),
                                   membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    c = await _get_contract_or_404(contract_id, membership.tenant_id, db)
    _ensure_contract_not_superseded(c)  # B9: neue Version hebt alte Freigabe auf -- hier technisch erzwungen
    a = await _get_contract_action_or_404(action_id, c.id, db)
    if a.contract_version_at_action != c.version:
        raise HTTPException(400, f"Freigabe ungueltig: Aktion wurde fuer Vertragsversion {a.contract_version_at_action} "
                                   f"entworfen, aktuelle Version ist {c.version}. Bitte neu entwerfen.")

    if a.kind == "send_draft":
        body_for_hash = c.body_text  # IMMER der aktuelle Vertragstext, nicht der zum Entwurfszeitpunkt eingefrorene
    else:
        body_for_hash = a.rendered_body

    if a.status == ContractActionStatus.draft:
        h = _compute_hash(a.recipient_email, a.rendered_subject, body_for_hash, a.proposed_clause_text)
        a.payload_hash = h
        a.status = ContractActionStatus.approved
        db.add(ContractApproval(action_id=a.id, approved_by=str(user.id), payload_hash=h))
        await db.commit()
    elif a.status != ContractActionStatus.approved:
        raise HTTPException(400, f"Aktion im Status '{a.status.value}' kann nicht freigegeben/gesendet werden.")

    await db.refresh(a)
    recomputed = _compute_hash(a.recipient_email, a.rendered_subject, body_for_hash, a.proposed_clause_text)
    if recomputed != a.payload_hash:
        raise HTTPException(400, "Freigabe ungueltig: Inhalt wurde nach Freigabe veraendert (Hash-Mismatch). Versand verweigert.")

    send_result = send_negotiation_email(to_email=a.recipient_email, subject=a.rendered_subject,
                                          body=body_for_hash, from_name="NegotiateX.ai")
    if not send_result.get("sent"):
        raise HTTPException(502, f"Versand fehlgeschlagen: {send_result.get('message')}")

    a.status = ContractActionStatus.sent
    if a.kind == "send_draft":
        await _contract_transition(db, c, ContractStatus.sent, actor=str(user.id), reason="Vertragsentwurf versendet.")
    await db.commit()
    return {"sent": True, "contract_status": c.status.value, "payload_hash": a.payload_hash}


@contracts_router.post("/{contract_id}/actions/{action_id}/reject")
async def reject_contract_action(contract_id: str, action_id: str, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    c = await _get_contract_or_404(contract_id, membership.tenant_id, db)
    a = await _get_contract_action_or_404(action_id, c.id, db)
    if a.status not in (ContractActionStatus.draft, ContractActionStatus.approved):
        raise HTTPException(400, f"Aktion im Status '{a.status.value}' kann nicht abgelehnt werden.")
    a.status = ContractActionStatus.rejected
    await db.commit()
    return {"status": "rejected"}


class ContractReturnPayload(BaseModel):
    returned_text: str


_LEGAL_CATEGORIES = {"liability", "ip", "data_protection", "term_termination"}


@contracts_router.post("/{contract_id}/return")
async def return_contract(contract_id: str, payload: ContractReturnPayload, user=Depends(get_current_user),
                           membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """B9 Rechtsklauseln + Kaufmaennische Punkte: vergleicht den
    zurueckgekommenen Text gegen den gesendeten Vertragstext (einfacher,
    ehrlicher Text-Diff -- siehe services/contract_diff.py). Jede erkannte
    Aenderung an Haftung/IP/Datenschutz/Laufzeit routet auf
    legal_review_required (eigener Status, getrennt von einem rein
    kaufmaennischen Redline). JEDE Aenderung -- ob innerhalb des Mandats oder
    nicht -- geht an einen Menschen; es gibt KEINE automatische Annahme."""
    c = await _get_contract_or_404(contract_id, membership.tenant_id, db)
    _ensure_contract_not_superseded(c)
    if c.status != ContractStatus.sent:
        raise HTTPException(400, f"Vertrag im Status '{c.status.value}' erwartet keinen Ruecklauf.")

    redline = detect_redline(c.body_text, payload.returned_text)
    clause_categories = classify_clause_changes(c.body_text, payload.returned_text) if redline else []
    legal_hit = any(cat in _LEGAL_CATEGORIES for cat in clause_categories)

    c.returned_text = payload.returned_text
    c.redline_detected = redline
    c.redline_clause_categories = clause_categories

    if legal_hit:
        c.legal_review_required = True
        await _contract_transition(db, c, ContractStatus.legal_review_required, actor="system",
                                    reason=f"Ruecklauf aendert rechtlich relevante Klauseln ({clause_categories}) -- Legal-Review erforderlich, keine automatische Annahme.")
    elif redline:
        c.commercial_review_required = True
        await _contract_transition(db, c, ContractStatus.commercial_review_required, actor="system",
                                    reason="Ruecklauf weicht kaufmaennisch vom gesendeten Entwurf ab -- "
                                           "Entscheidung durch Einkauf erforderlich (auch wenn innerhalb des Mandats), keine automatische Annahme.")
    else:
        await _contract_transition(db, c, ContractStatus.returned, actor="system",
                                    reason="Ruecklauf identisch zum gesendeten Entwurf -- bereit fuer Abschluss (/finalize).")
    await db.commit()
    result = _contract_to_dict(c)
    result["note"] = "Redline-Erkennung ist ein einfacher, ehrlicher Textvergleich -- kein juristisches Verstehen der Aenderung."
    return result


class NewVersionPayload(BaseModel):
    body_text: str
    reason: str


@contracts_router.post("/{contract_id}/new-version")
async def new_contract_version(contract_id: str, payload: NewVersionPayload, user=Depends(get_current_user),
                                membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """'Eine neue Vertragsversion hebt die bisherige Freigabe auf' -- technisch
    durchgesetzt: die ALTE Zeile bekommt superseded_by_version gesetzt, danach
    blockiert _ensure_contract_not_superseded JEDEN Freigabe-/Versandversuch
    auf der alten Zeile (siehe approve_contract_action)."""
    old = await _get_contract_or_404(contract_id, membership.tenant_id, db)
    _ensure_contract_not_superseded(old)

    new = Contract(
        tenant_id=membership.tenant_id, lineage_id=old.lineage_id, version=old.version + 1,
        sourcing_request_id=old.sourcing_request_id, case_id=old.case_id, rfq_id=old.rfq_id,
        offer_id=old.offer_id, template_id=old.template_id, supplier_candidate_id=old.supplier_candidate_id,
        status=ContractStatus.draft, body_text=payload.body_text,
        mandate_price_cap=old.mandate_price_cap, mandate_payment_terms_days_max=old.mandate_payment_terms_days_max,
        mandate_delivery_date_latest=old.mandate_delivery_date_latest, created_by=str(user.id),
    )
    db.add(new)
    await db.flush()
    old.superseded_by_version = new.version
    db.add(ContractEvent(contract_id=old.id, tenant_id=old.tenant_id, from_status=old.status.value,
                          to_status=old.status.value, actor=str(user.id),
                          reason=f"Durch Version {new.version} ersetzt: {payload.reason}"))
    db.add(ContractEvent(contract_id=new.id, tenant_id=new.tenant_id, from_status=None, to_status=ContractStatus.draft.value,
                          actor=str(user.id), reason=f"Neue Version {new.version} angelegt (ersetzt Version {old.version}): {payload.reason}"))
    await db.commit()
    await db.refresh(new)
    result = _contract_to_dict(new)
    result["superseded_contract_id"] = str(old.id)
    result["note"] = "Neue Vertragsversion erstellt -- jede Freigabe/Aktion der vorherigen Version ist ab sofort ungueltig."
    return result


@contracts_router.post("/{contract_id}/finalize")
async def finalize_contract(contract_id: str, user=Depends(get_current_user),
                             membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """B9 Abschluss: konsolidierte Version + Redline-Zusammenfassung +
    offene Punkte + 'Signaturpaket' (= Referenz auf das final zu
    unterzeichnende Dokument + Checkliste). Setzt status=approved_by_buyer --
    das ist AUSDRUECKLICH NICHT 'signiert'. Playbook-Zitat: 'Im Test und in
    Version 1 keine autonome rechtsverbindliche Annahme.'"""
    c = await _get_contract_or_404(contract_id, membership.tenant_id, db)
    _ensure_contract_not_superseded(c)
    if c.status not in (ContractStatus.returned, ContractStatus.commercial_review_required):
        raise HTTPException(400, f"Vertrag im Status '{c.status.value}' kann nicht finalisiert werden "
                                   f"(Ruecklauf ohne offene rechtliche Pruefung erforderlich).")

    open_points = []
    if c.commercial_review_required:
        open_points.append("Kaufmaennische Abweichung im Ruecklauf wurde erkannt -- Einkaufsentscheidung dokumentiert, aber nicht rueckgaengig geprueft.")
    if c.redline_detected:
        open_points.append("Ruecklauf wich textlich vom gesendeten Entwurf ab (siehe redline_clause_categories).")
    if not open_points:
        open_points.append("Keine offenen Punkte erkannt.")
    c.open_points_json = open_points

    await _contract_transition(db, c, ContractStatus.approved_by_buyer, actor=str(user.id),
                                reason="Vertrag konsolidiert und vom Einkauf bestaetigt (noch keine rechtsverbindliche Unterschrift im System).")
    await db.commit()
    return {
        "contract_id": str(c.id), "status": c.status.value,
        "consolidated_body_text": c.body_text,
        "redline_summary": {"redline_detected": c.redline_detected, "clause_categories": c.redline_clause_categories or []},
        "open_points": open_points,
        "signature_package": {
            "document_reference": f"contract:{c.id}:v{c.version}",
            "checklist": [
                "Preis/Zahlungsbedingungen/Liefertermin innerhalb Mandat oder gesondert genehmigt",
                "Keine offenen Rechtsklauseln-Abweichungen (legal_review_required=False)" if not c.legal_review_required else "ACHTUNG: legal_review_required war gesetzt -- vor Unterschrift pruefen",
                "Parteien/Vertragsversion korrekt",
            ],
        },
        "note": "Im Test und in Version 1 keine autonome rechtsverbindliche Annahme. Eine tatsaechliche "
                "Unterschrift wird ausschliesslich ueber POST /contracts/{id}/human-confirmed-signed als "
                "menschliche Bestaetigung erfasst -- das ist eine Status-Bestaetigung, keine rechtsverbindliche "
                "elektronische Signatur im System.",
    }


class HumanConfirmSigned(BaseModel):
    confirmed_by_name: str
    note: Optional[str] = None


@contracts_router.post("/{contract_id}/human-confirmed-signed")
async def human_confirmed_signed(contract_id: str, payload: HumanConfirmSigned, user=Depends(get_current_user),
                                  membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """Der EINZIGE Code-Pfad im gesamten Modul, der
    Contract.human_confirmed_signed/status auf 'human_confirmed_signed'
    setzen kann. Verlangt zwingend status==approved_by_buyer (= /finalize
    bereits durchlaufen). Dies ist eine reine Status-Bestaetigung ('ein
    Mensch hat bestaetigt, dass ausserhalb des Systems unterschrieben wurde'),
    KEINE rechtsverbindliche elektronische Signatur -- identische Ehrlichkeit
    wie models_sourcing.NDA.approve_nda."""
    c = await _get_contract_or_404(contract_id, membership.tenant_id, db)
    _ensure_contract_not_superseded(c)
    if c.status != ContractStatus.approved_by_buyer:
        raise HTTPException(400, "Vertrag kann nur nach /finalize (status=approved_by_buyer) als "
                                   "'menschlich signiert bestaetigt' markiert werden.")

    c.human_confirmed_signed = True
    c.human_confirmed_signed_by = payload.confirmed_by_name
    c.human_confirmed_signed_at = datetime.utcnow()
    await _contract_transition(db, c, ContractStatus.human_confirmed_signed, actor=str(user.id),
                                reason=f"Menschlich bestaetigt signiert von '{payload.confirmed_by_name}'. {payload.note or ''}")
    await db.commit()
    return {
        "contract_id": str(c.id), "status": c.status.value, "human_confirmed_signed": True,
        "note": "Status-Bestaetigung, KEINE rechtsverbindliche Unterschrift im System.",
    }
