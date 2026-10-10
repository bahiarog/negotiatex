"""
Vorhaben -- der gemeinsame Kern von Kunden- und Arbeitsbereich.

Kundenbereich (Rolle customer): Übersicht, Meine Vorhaben, Einsparungen,
Entscheidungen; ein Vorhaben hat vier Ansichten (Überblick, Angebote,
Entscheidungen, Dokumente & Verlauf). Arbeitsbereich (procurement/admin) sieht
dieselben Vorhaben plus Bearbeitungskontext. Keine Datenkopien: beide Sichten
lesen dieselben Tabellen und dieselbe Ergebnisberechnung
(services/project_results.py).

Rechte werden hier je Endpunkt geprueft (access.py), nicht nur ueber die
Navigation:
  - Sichtbarkeit: Arbeitsbereich alle Vorhaben des Mandanten; Kunden eigene
    oder fuer den Mandanten freigegebene Vorhaben.
  - Entscheidungen (Auftrag erteilen, Anbieter beauftragen): nur mit
    Entscheidungsbefugnis -- Admin-Sein genuegt nicht.
  - Nachrichten an Dienstleister freigeben: Procurement-Experte oder
    entscheidungsbefugter Nutzer, und nur Entwuerfe, die zu DIESEM Vorhaben
    gehoeren (Freigabe laeuft ueber /projects/{id}/drafts/..., die
    Modul-Schnittstellen selbst sind dem Arbeitsbereich vorbehalten).
  - NDA-Pruefung und Zuweisung des Verantwortlichen: Arbeitsbereich.

Versand bleibt in den Modulen (hash-gebundene Freigabe); ein Zuschlag
erzeugt nur Entwuerfe fuer Zu- und Absagen.
"""
import asyncio
import logging
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from access import Access, get_access, require_decider, require_message_approver
from database import get_db
from models_contracts import (
    RFQ, RFQAction, RFQActionStatus, RFQInvitation, RFQOffer, RFQStatus, OfferStatus,
)
from models_mdc import MDCDocument, MDCDocumentVersion, MDCSupplier
from models_negotiation import NegotiationAction, NegotiationActionStatus, NegotiationStrategy
from models_projects import OfferReview, Project, ProjectEvent, ProjectStatus
from models_sourcing import (
    NDA, NDAStatus, CandidateStatus, OutreachAction, OutreachActionStatus, OutreachMessage,
    SourcingRequest, SourcingRequestStatus, SupplierCandidate,
)
from models_v2 import Case, CaseEvent, CaseStatus, Membership, SupplierV2, Tenant
from routers.auth import User
from services.project_results import compute_result, offer_total
from services.projects import add_event

logger = logging.getLogger(__name__)
router = APIRouter()

PHASE_LABEL = {
    "briefing": "Bedarf erfassen", "sourcing": "Dienstleister ansprechen", "collecting_offers": "Angebote einholen",
    "evaluating": "Angebote prüfen & verhandeln", "decision": "Entscheidung", "awarded": "Beauftragt", "cancelled": "Abgebrochen",
}
STATE_LABEL = {"draft": "Entwurf", "in_progress": "In Bearbeitung", "waiting_for_you": "Wartet auf Sie", "done": "Abgeschlossen"}
CANDIDATE_LABEL = {
    "found": "vorgeschlagen", "shortlisted": "ausgewählt", "contacted": "angeschrieben", "responded": "hat geantwortet",
    "interested": "interessiert", "declined": "abgesagt", "onboarding_pending": "Unterlagen in Arbeit",
    "nda_review": "Vertraulichkeit in Klärung", "nda_approved": "Vertraulichkeit geklärt", "qualified": "qualifiziert",
    "rejected": "ausgeschieden",
}
REQUIRED_TO_START = {"title": "Bitte einen Titel angeben.", "description": "Bitte den Bedarf beschreiben.",
                     "budget_ceiling": "Bitte eine Budget-Obergrenze angeben – sie ist der Verhandlungsrahmen."}

FIRST_CONTACT_SUBJECT = "{prefix}Anfrage: {titel} / {token}"
FIRST_CONTACT_BODY = (
    "Guten Tag{anrede},\n\n"
    "wir koordinieren fuer einen Auftraggeber eine Anfrage im Bereich {leistung}:\n\n{teaser}\n\n"
    "{termin}"
    "Haetten Sie grundsaetzlich Interesse und Kapazitaet, dafuer ein Angebot abzugeben? Bitte antworten Sie "
    "kurz auf diese E-Mail und nennen Sie uns Ihren zustaendigen Ansprechpartner. Danach erhalten Sie die "
    "vollstaendigen Unterlagen.\n\n"
    "Diese Anfrage ist keine Bestellung.\n\n"
    "Freundliche Gruesse,\nNegotiateX – KI-gestuetzte Beschaffungskoordination{testnote}"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _v(x):
    return x.value if hasattr(x, "value") else x


def _s(x):
    return str(x) if x is not None else None


def field_error(field: str, message: str, status: int = 422):
    return HTTPException(status, {"field": field, "message": message})


def _visible_filter(access: Access):
    """Kunden sehen eigene oder fuer den Mandanten freigegebene Vorhaben."""
    if access.workspace:
        return Project.tenant_id == access.tenant_id
    return (Project.tenant_id == access.tenant_id) & or_(
        Project.created_by == str(access.user.id), Project.shared_with_tenant == True)  # noqa: E712


async def _get_project(project_id: str, access: Access, db: AsyncSession) -> Project:
    try:
        pid = uuid.UUID(str(project_id))
    except ValueError:
        raise HTTPException(404, "Vorhaben nicht gefunden.")
    p = (await db.execute(select(Project).where(Project.id == pid, _visible_filter(access)))).scalar_one_or_none()
    if not p:
        raise HTTPException(404, "Vorhaben nicht gefunden.")
    return p


def _can_edit(p: Project, access: Access) -> bool:
    return access.workspace or p.created_by == str(access.user.id) or access.can_decide


async def _user_names(db: AsyncSession, ids) -> dict:
    ids = [i for i in {i for i in ids if i}]
    if not ids:
        return {}
    rows = (await db.execute(select(User).where(User.id.in_(ids)))).scalars().all()
    return {u.id: (u.name or u.email) for u in rows}


def _dec(v, field: str = "betrag") -> Optional[Decimal]:
    if v is None or v == "":
        return None
    try:
        d = Decimal(str(v).replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        raise field_error(field, "Bitte eine Zahl angeben (z.B. 12000).")
    if d < 0:
        raise field_error(field, "Der Betrag darf nicht negativ sein.")
    return d


async def _read_upload(file: UploadFile, field: str = "file", max_mb: int = 15) -> bytes:
    data = await file.read()
    if not data:
        raise field_error(field, "Die Datei ist leer.")
    if len(data) > max_mb * 1024 * 1024:
        raise field_error(field, f"Die Datei ist zu groß (max. {max_mb} MB).")
    return data


def _project_dict(p: Project, names: dict | None = None) -> dict:
    names = names or {}
    return {
        "id": str(p.id), "title": p.title, "entry_path": p.entry_path, "status": _v(p.status),
        "phase_label": PHASE_LABEL.get(_v(p.status), _v(p.status)),
        "service_type": p.service_type, "description": p.description, "must_criteria": p.must_criteria_json or {},
        "conditions_text": p.conditions_text, "budget_target": _s(p.budget_target), "budget_ceiling": _s(p.budget_ceiling),
        "currency": p.currency, "needed_by": p.needed_by.isoformat() if p.needed_by else None, "region": p.region,
        "delivery_location": p.delivery_location, "quantity": _s(p.quantity), "unit": p.unit,
        "briefing_source": p.briefing_source, "briefing_document_id": _s(p.briefing_document_id),
        "offer_document_id": _s(p.offer_document_id), "incumbent_company": p.incumbent_company,
        "incumbent_email": p.incumbent_email, "incumbent_contact": p.incumbent_contact,
        "open_questions": p.open_questions_json or [], "seek_alternatives": p.seek_alternatives,
        "customer_email": p.customer_email, "notify_milestones": p.notify_milestones,
        "responsible": names.get(p.responsible_user_id) if p.responsible_user_id else None,
        "responsible_user_id": _s(p.responsible_user_id), "owner": names.get(_uuid_or_none(p.created_by)),
        "shared_with_tenant": p.shared_with_tenant,
        "mandate_confirmed_at": p.mandate_confirmed_at.isoformat() if p.mandate_confirmed_at else None,
        "awarded_offer_id": _s(p.awarded_offer_id), "awarded_at": p.awarded_at.isoformat() if p.awarded_at else None,
        "decision_note": p.decision_note, "archived_at": p.archived_at.isoformat() if p.archived_at else None,
        "invoiced_amount": _s(p.invoiced_amount), "invoiced_at": p.invoiced_at.isoformat() if p.invoiced_at else None,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def _uuid_or_none(s):
    try:
        return uuid.UUID(str(s))
    except (ValueError, TypeError):
        return None


async def _fresh(db: AsyncSession, p: Project) -> dict:
    """Nach commit: serverseitig gesetzte Felder (updated_at) neu laden."""
    await db.refresh(p)
    names = await _user_names(db, [p.responsible_user_id, _uuid_or_none(p.created_by)])
    return _project_dict(p, names)


def _missing_for_start(p: Project) -> dict:
    missing = {}
    if not (p.title or "").strip() or (p.title or "").startswith("Neues Vorhaben"):
        missing["title"] = REQUIRED_TO_START["title"]
    if not (p.description or "").strip():
        missing["description"] = REQUIRED_TO_START["description"]
    if not p.budget_ceiling:
        missing["budget_ceiling"] = REQUIRED_TO_START["budget_ceiling"]
    if p.entry_path == "existing_offer":
        if not p.offer_document_id:
            missing["offer_file"] = "Bitte das vorhandene Angebot hochladen."
        if not p.incumbent_company:
            missing["incumbent_company"] = "Bitte den Anbieter angeben."
        if not p.incumbent_email or "@" not in (p.incumbent_email or ""):
            missing["incumbent_email"] = "Bitte eine gültige E-Mail-Adresse des Anbieters angeben."
    return missing


async def _ensure_request(db: AsyncSession, p: Project, user_id: str) -> SourcingRequest:
    req = None
    if p.sourcing_request_id:
        req = (await db.execute(select(SourcingRequest).where(SourcingRequest.id == p.sourcing_request_id))).scalar_one_or_none()
    teaser = f"{p.service_type or 'Dienstleistung'}" + (f", Region {p.region}" if p.region else "")
    if req is None:
        req = SourcingRequest(
            tenant_id=p.tenant_id, title=p.title, status=SourcingRequestStatus.draft,
            bedarf_text=p.description or p.title, must_criteria_json=p.must_criteria_json or {},
            region=p.region, delivery_location=p.delivery_location, delivery_capability_confirmed=True,
            budget_target=p.budget_target, budget_ceiling=p.budget_ceiling or Decimal("0"), currency=p.currency or "EUR",
            max_candidates_total=15, max_contacted=10, public_teaser_text=teaser,
            responsible_procurement=p.customer_email, created_by=str(user_id),
        )
        db.add(req)
        await db.flush()
        p.sourcing_request_id = req.id
    else:
        req.title, req.bedarf_text = p.title, p.description or p.title
        req.must_criteria_json = p.must_criteria_json or {}
        req.region, req.delivery_location = p.region, p.delivery_location
        req.budget_target, req.budget_ceiling = p.budget_target, p.budget_ceiling or req.budget_ceiling
        req.public_teaser_text = teaser
    return req


async def _ensure_rfq(db: AsyncSession, p: Project, req: SourcingRequest, user_id: str) -> RFQ:
    rfq = None
    if p.rfq_id:
        rfq = (await db.execute(select(RFQ).where(RFQ.id == p.rfq_id))).scalar_one_or_none()
    if rfq is None:
        rfq = (await db.execute(select(RFQ).where(RFQ.sourcing_request_id == req.id).order_by(RFQ.created_at))).scalars().first()
    if rfq is None:
        spec = [p.description or p.title]
        if p.must_criteria_json:
            spec.append("Muss-Kriterien:\n" + "\n".join(f"- {k}: {v}" for k, v in p.must_criteria_json.items()))
        if p.conditions_text:
            spec.append(f"Konditionen: {p.conditions_text}")
        if p.needed_by:
            spec.append(f"Benoetigt bis: {p.needed_by:%d.%m.%Y}")
        rfq = RFQ(tenant_id=p.tenant_id, sourcing_request_id=req.id, spec_text="\n\n".join(spec),
                  expected_quantity=p.quantity, expected_unit=p.unit, currency=p.currency or "EUR",
                  deadline=datetime.utcnow() + timedelta(days=3), test_mode=True, created_by=str(user_id))
        db.add(rfq)
        await db.flush()
    p.rfq_id = rfq.id
    return rfq


def _first_contact(p: Project, cand: SupplierCandidate) -> tuple[str, str]:
    teaser = (p.description or p.title or "")[:900]
    termin = f"Gewuenschter Zeitraum: bis {p.needed_by:%d.%m.%Y}.\n\n" if p.needed_by else ""
    subject = FIRST_CONTACT_SUBJECT.format(prefix="[TEST – ]", titel=(p.title or "")[:70], token=str(cand.id)[:8])
    body = FIRST_CONTACT_BODY.format(
        anrede=f" {cand.contact_name}" if cand.contact_name else "", leistung=p.service_type or "Dienstleistung",
        teaser=teaser, termin=termin, testnote=".\nTestlauf, keine Beauftragung.",
    )
    return subject, body


async def _project_candidates(db: AsyncSession, p: Project) -> list[SupplierCandidate]:
    if not p.sourcing_request_id:
        return []
    return list((await db.execute(select(SupplierCandidate).where(SupplierCandidate.sourcing_request_id == p.sourcing_request_id)
                                  .order_by(SupplierCandidate.created_at))).scalars().all())


async def _project_rfqs(db: AsyncSession, p: Project) -> list[RFQ]:
    if not p.sourcing_request_id:
        return []
    return list((await db.execute(select(RFQ).where(RFQ.sourcing_request_id == p.sourcing_request_id))).scalars().all())


async def _tenant(db: AsyncSession, tenant_id) -> Optional[Tenant]:
    return (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Entwuerfe (ausgehende Nachrichten) je Vorhaben
# ---------------------------------------------------------------------------

def _draft(kind, label, cand_name, a, scope_id, extra=None):
    d = {"kind": kind, "label": label, "id": str(a.id), "scope_id": str(scope_id), "supplier": cand_name,
         "recipient": getattr(a, "recipient_email", None), "subject": getattr(a, "rendered_subject", None),
         "body": getattr(a, "rendered_body", None),
         "created_at": a.created_at.isoformat() if getattr(a, "created_at", None) else None}
    d.update(extra or {})
    return d


async def _pending_drafts(db: AsyncSession, p: Project) -> list[dict]:
    cands = await _project_candidates(db, p)
    by_id = {c.id: c for c in cands}
    out = []
    if by_id:
        for a in (await db.execute(select(OutreachAction).where(OutreachAction.supplier_candidate_id.in_(list(by_id)),
                                                                OutreachAction.status == OutreachActionStatus.draft)
                                   .order_by(OutreachAction.created_at))).scalars().all():
            label = {"first_contact": "Erstanfrage an Dienstleister", "reminder": "Erinnerung an Dienstleister",
                     "stammdaten_invite": "Link für Firmendaten"}.get(a.kind, a.kind)
            out.append(_draft("outreach", label, by_id[a.supplier_candidate_id].company_name, a, a.supplier_candidate_id))
        for n in (await db.execute(select(NDA).where(NDA.supplier_candidate_id.in_(list(by_id)),
                                                     NDA.status.in_([NDAStatus.draft, NDAStatus.returned, NDAStatus.review_required, NDAStatus.verified])))).scalars().all():
            st = _v(n.status)
            label = {"draft": "Vertraulichkeitsvereinbarung versenden", "returned": "Rücklauf Vertraulichkeitsvereinbarung prüfen",
                     "review_required": "Abweichung in Vertraulichkeitsvereinbarung prüfen", "verified": "Vertraulichkeitsvereinbarung final freigeben"}[st]
            c = by_id[n.supplier_candidate_id]
            out.append({"kind": "nda", "label": label, "id": str(n.id), "scope_id": str(c.id), "supplier": c.company_name,
                        "recipient": c.contact_email, "subject": f"NDA {n.template_version}",
                        "body": n.draft_text if st == "draft" else (n.returned_text or ""), "nda_status": st,
                        "workspace_only": st != "draft",
                        "created_at": n.updated_at.isoformat() if n.updated_at else None})
    for rfq in await _project_rfqs(db, p):
        for a in (await db.execute(select(RFQAction).where(RFQAction.rfq_id == rfq.id, RFQAction.status == RFQActionStatus.draft)
                                   .order_by(RFQAction.created_at))).scalars().all():
            label = {"invite": "Angebotsanfrage", "receipt_confirmation": "Eingangsbestätigung", "clarification_broadcast": "Klarstellung",
                     "award_notice": "Zusage an Dienstleister", "decline_notice": "Absage an Dienstleister"}.get(a.kind, a.kind)
            c = by_id.get(a.supplier_candidate_id)
            out.append(_draft("rfq", label, c.company_name if c else "", a, rfq.id))
    for offer_id, case_id in (p.negotiations_json or {}).items():
        cid = _uuid_or_none(case_id)
        if not cid:
            continue
        for a in (await db.execute(select(NegotiationAction).where(NegotiationAction.case_id == cid,
                                                                   NegotiationAction.status == NegotiationActionStatus.draft))).scalars().all():
            offer = (await db.execute(select(RFQOffer).where(RFQOffer.id == _uuid_or_none(offer_id)))).scalar_one_or_none()
            c = by_id.get(offer.supplier_candidate_id) if offer else None
            label = "Preisvorschlag (Verhandlung)" if _v(a.action_type) == "propose_price" else "Erinnerung (Verhandlung)"
            out.append(_draft("negotiation", label, c.company_name if c else "", a, cid,
                              {"price": _s(a.proposed_price_net), "warnings": a.open_questions or []}))
    # Doppelte Eintraege (gleicher Fall unter alter und neuer Angebotsversion) entfernen
    seen, unique = set(), []
    for d in out:
        if (d["kind"], d["id"]) not in seen:
            seen.add((d["kind"], d["id"]))
            unique.append(d)
    return unique


def _drafts_for(access: Access, drafts: list) -> list:
    """Welche Entwuerfe darf DIESER Nutzer freigeben?"""
    out = []
    for d in drafts:
        if d.get("workspace_only"):
            if access.procurement:
                out.append(d)
        elif access.can_approve_messages:
            out.append(d)
    return out


# ---------------------------------------------------------------------------
# Angebote, Zustand, naechster Schritt
# ---------------------------------------------------------------------------

async def _offers(db: AsyncSession, p: Project, cands: dict) -> list[dict]:
    out = []
    for rfq in await _project_rfqs(db, p):
        offers = (await db.execute(select(RFQOffer).where(RFQOffer.rfq_id == rfq.id).order_by(RFQOffer.received_at))).scalars().all()
        for o in offers:
            if _v(o.status) != "submitted":
                continue
            history = [x for x in offers if x.supplier_candidate_id == o.supplier_candidate_id and x.id != o.id]
            first = min(history + [o], key=lambda x: x.version)
            rv = (await db.execute(select(OfferReview).where(OfferReview.offer_id.in_([h.id for h in history] + [o.id]))
                                   .order_by(desc(OfferReview.created_at)))).scalars().first()
            doc = (await db.execute(select(MDCDocument).where(MDCDocument.rfq_offer_id.in_([x.id for x in history] + [o.id])))).scalars().first()
            case_id = None
            for oid in [str(x.id) for x in history] + [str(o.id)]:
                case_id = (p.negotiations_json or {}).get(oid) or case_id
            neg = None
            if case_id:
                case = (await db.execute(select(Case).where(Case.id == uuid.UUID(case_id)))).scalar_one_or_none()
                strat = (await db.execute(select(NegotiationStrategy).where(NegotiationStrategy.case_id == uuid.UUID(case_id)))).scalar_one_or_none()
                if case:
                    from routers.negotiation import _latest_inbound_price
                    latest = await _latest_inbound_price(case.id, db)
                    neg = {"case_id": case_id, "status": _v(case.status), "latest_counter": _s(latest),
                           "round1": _s(strat.round1_price_net) if strat else None, "round2": _s(strat.round2_price_net) if strat else None,
                           "start": _s(strat.starting_price_net) if strat else None}
            c = cands.get(o.supplier_candidate_id)
            total, first_total = offer_total(o, p.quantity), offer_total(first, p.quantity)
            findings = (rv.findings_json or []) if rv else []
            out.append({
                "id": str(o.id), "version": o.version, "supplier": c.company_name if c else "",
                "candidate_id": str(o.supplier_candidate_id), "is_incumbent": p.incumbent_candidate_id == o.supplier_candidate_id,
                "unit_price": _s(o.unit_price), "quantity": _s(o.quantity), "currency": o.currency,
                "total": _s(total), "first_total": _s(first_total) if o.version > 1 else None,
                "improvement": _s(first_total - total) if (o.version > 1 and total is not None and first_total is not None) else None,
                "delivery_date": o.delivery_date, "payment_terms": o.payment_terms,
                "offer_validity_until": o.offer_validity_until.date().isoformat() if o.offer_validity_until else None,
                "scope_note": o.scope_note, "spec_confirmed": o.spec_confirmed,
                "comparable": _v(o.comparability_flag) == "comparable" and total is not None,
                "comparability_note": o.comparability_note,
                "within_budget": (total <= p.budget_ceiling) if (total is not None and p.budget_ceiling) else None,
                "review": {"status": rv.status, "findings": findings,
                           "critical": sum(1 for f in findings if f.get("severity") == "critical"),
                           "warnings": sum(1 for f in findings if f.get("severity") == "warning"),
                           "at": rv.created_at.isoformat() if rv.created_at else None} if rv else None,
                "document_id": str(doc.id) if doc else None, "negotiation": neg,
                "received_at": o.received_at.isoformat() if o.received_at else None,
                "awarded": p.awarded_offer_id == o.id,
            })
    out.sort(key=lambda x: (not x["comparable"], x["total"] is None, Decimal(x["total"]) if x["total"] else 0))
    return out


def _customer_state(p: Project, my_decisions: list) -> str:
    st = _v(p.status)
    if st == "briefing":
        return "draft"
    if st in ("awarded", "cancelled"):
        return "done"
    return "waiting_for_you" if my_decisions else "in_progress"


def _stage(p: Project, offers: list, cands: list) -> int:
    st = _v(p.status)
    if st == "awarded":
        return 4
    if offers:
        return 3
    if cands:
        return 2
    return 1 if st != "briefing" else 0


def _decisions(p: Project, access: Access, drafts: list, offers: list, cands: list) -> list[dict]:
    """Aufgaben DIESES Nutzers zu einem Vorhaben (Entscheidungen-Ansicht)."""
    st = _v(p.status)
    items = []
    if st == "briefing" and _can_edit(p, access):
        missing = _missing_for_start(p)
        if missing:
            items.append({"kind": "complete_info", "title": "Angaben ergänzen", "context": " ".join(missing.values()),
                          "tab": "neu"})
        if not missing and access.can_decide:
            items.append({"kind": "confirm_order", "title": "Auftrag prüfen und erteilen",
                          "context": "Zusammenfassung und Verhandlungsmandat bestätigen – danach beginnt der Agent.", "tab": "neu"})
    if st in ("awarded", "cancelled"):
        for d in _drafts_for(access, drafts):
            items.append({"kind": "approve_message", "title": d["label"], "context": f"{d['supplier']} · {d['recipient'] or ''}",
                          "tab": "entscheidungen", "draft": d})
        return items
    if st != "briefing":
        for d in _drafts_for(access, drafts):
            items.append({"kind": "approve_message", "title": d["label"] + (" freigeben" if not d.get("workspace_only") else ""),
                          "context": f"{d['supplier']} · {d['recipient'] or ''}", "tab": "entscheidungen", "draft": d})
        active_cands = [c for c in cands if _v(c.status) not in ("rejected", "declined")]
        if not active_cands and (access.can_decide or access.procurement):
            items.append({"kind": "choose_suppliers", "title": "Dienstleister für die Anfrage auswählen",
                          "context": "Der Agent hat Vorschläge aus Ihrem Bestand vorbereitet.", "tab": "entscheidungen"})
        if offers and access.can_decide:
            items.append({"kind": "choose_offer", "title": "Anbieter wählen",
                          "context": f"{len(offers)} Angebot(e) eingegangen, {sum(1 for o in offers if o['comparable'])} vergleichbar.",
                          "tab": "angebote"})
    return items


def _next_action(p: Project, access: Access, decisions: list, offers: list, cands: list, drafts_all: list) -> dict:
    """Genau eine naechste Hauptaktion -- oder im Wartezustand: wer handelt als Naechstes."""
    st = _v(p.status)
    kinds = [d["kind"] for d in decisions]
    if st == "briefing":
        if "complete_info" in kinds:
            return {"label": "Angaben ergänzen", "tab": "neu", "who": "you",
                    "text": "Ihr Entwurf ist gespeichert. Es fehlen noch Angaben, bevor der Auftrag erteilt werden kann."}
        if "confirm_order" in kinds:
            return {"label": "Auftrag prüfen", "tab": "neu", "who": "you",
                    "text": "Alle Angaben sind vollständig. Prüfen Sie Zusammenfassung und Verhandlungsmandat und erteilen Sie den Auftrag."}
        return {"label": None, "who": "decider", "text": "Der Auftrag muss von einer entscheidungsbefugten Person erteilt werden."}
    if st == "cancelled":
        return {"label": None, "who": "nobody", "text": "Das Vorhaben wurde abgebrochen."}
    if st == "awarded":
        if "approve_message" in kinds:
            return {"label": "Zu- und Absagen freigeben", "tab": "entscheidungen", "who": "you",
                    "text": "Ihre Entscheidung ist gespeichert. Die Nachrichten an die Dienstleister warten auf Ihre Freigabe."}
        if p.invoiced_amount is None:
            return {"label": None, "who": "you_optional",
                    "text": "Beauftragt. Sobald die Rechnung vorliegt, können Sie den abgerechneten Betrag erfassen – dann wird die Einsparung als realisiert ausgewiesen."}
        return {"label": None, "who": "nobody", "text": "Abgeschlossen."}
    if "approve_message" in kinds:
        n = kinds.count("approve_message")
        return {"label": "Entscheidung prüfen", "tab": "entscheidungen", "who": "you",
                "text": f"{n} Nachricht(en) an Dienstleister warten auf Ihre Freigabe."}
    if "choose_suppliers" in kinds:
        return {"label": "Dienstleister auswählen", "tab": "entscheidungen", "who": "you",
                "text": "Der Agent hat passende Dienstleister aus Ihrem Bestand vorgeschlagen. Wählen Sie aus, wer angefragt wird."}
    ready = [o for o in offers if o["negotiation"] and o["negotiation"]["status"] == "READY_FOR_DECISION"]
    if ready and "choose_offer" in kinds:
        return {"label": "Entscheidung prüfen", "tab": "angebote", "who": "you", "text": "Die Verhandlung ist abgeschlossen."}
    if offers and "choose_offer" in kinds:
        waiting = [c for c in cands if _v(c.status) in ("contacted", "onboarding_pending", "nda_review", "nda_approved", "interested", "qualified")
                   and not any(o["candidate_id"] == str(c.id) for o in offers)]
        extra = f" Von {len(waiting)} weiteren Dienstleister(n) stehen Angebote noch aus." if waiting else ""
        return {"label": "Angebote vergleichen", "tab": "angebote", "who": "you",
                "text": f"{len(offers)} Angebot(e) liegen vor.{extra}"}
    # Wartezustand: wer handelt?
    if any(d.get("workspace_only") for d in drafts_all):
        return {"label": None, "who": "expert", "text": "Ihr Procurement-Experte prüft gerade eine Vertraulichkeitsvereinbarung. Sie müssen nichts tun."}
    if drafts_all and not access.can_approve_messages:
        return {"label": None, "who": "decider", "text": "Nachrichten an Dienstleister warten auf Freigabe durch eine berechtigte Person."}
    contacted = [c for c in cands if _v(c.status) in ("contacted", "responded")]
    in_docs = [c for c in cands if _v(c.status) in ("interested", "onboarding_pending", "nda_review", "nda_approved", "qualified")]
    if contacted:
        return {"label": None, "who": "suppliers", "text": f"Der Agent wartet auf Antworten von {len(contacted)} Dienstleister(n). Sie müssen nichts tun – wir melden uns per E-Mail."}
    if in_docs:
        return {"label": None, "who": "suppliers", "text": f"{len(in_docs)} Dienstleister bereiten Unterlagen bzw. ihr Angebot vor. Sie müssen nichts tun."}
    return {"label": None, "who": "agent", "text": "Der Agent arbeitet an Ihrem Vorhaben."}


async def _summary(db: AsyncSession, p: Project, access: Access) -> dict:
    """Kompakte Kennzahlen fuer Listen, Uebersicht und Entscheidungen."""
    cands = await _project_candidates(db, p)
    offers = await _offers(db, p, {c.id: c for c in cands})
    drafts = await _pending_drafts(db, p)
    decisions = _decisions(p, access, drafts, offers, cands)
    return {"cands": cands, "offers": offers, "drafts": drafts, "decisions": decisions,
            "state": _customer_state(p, decisions),
            "next": _next_action(p, access, decisions, offers, cands, drafts)}


def _list_row(p: Project, s: dict, names: dict, result: dict) -> dict:
    d = _project_dict(p, names)
    d.update({
        "state": s["state"], "state_label": STATE_LABEL[s["state"]],
        "offers_count": len(s["offers"]), "comparable_count": sum(1 for o in s["offers"] if o["comparable"]),
        "contacted_count": sum(1 for c in s["cands"] if _v(c.status) not in ("found", "shortlisted", "rejected")),
        "decisions_count": len(s["decisions"]), "next": s["next"], "result": result,
    })
    return d


# ---------------------------------------------------------------------------
# Kundenbereich: Uebersicht, Liste, Entscheidungen, Einsparungen
# ---------------------------------------------------------------------------

async def _visible_projects(db: AsyncSession, access: Access) -> list[Project]:
    return list((await db.execute(select(Project).where(_visible_filter(access)).order_by(desc(Project.updated_at)))).scalars().all())


@router.get("")
async def list_projects(q: Optional[str] = None, state: Optional[str] = None, archived: bool = False, mine: bool = False,
                        access: Access = Depends(get_access), db: AsyncSession = Depends(get_db)):
    rows = await _visible_projects(db, access)
    names = await _user_names(db, [p.responsible_user_id for p in rows] + [_uuid_or_none(p.created_by) for p in rows])
    tenant = await _tenant(db, access.tenant_id)
    out = []
    for p in rows:
        if bool(p.archived_at) != archived:
            continue
        if mine and str(access.user.id) not in (p.created_by, _s(p.responsible_user_id)):
            continue
        if q and q.lower() not in " ".join(x or "" for x in (p.title, p.service_type, p.description, p.incumbent_company)).lower():
            continue
        s = await _summary(db, p, access)
        if state and s["state"] != state:
            continue
        out.append(_list_row(p, s, names, await compute_result(db, p, tenant)))
    return out


@router.get("/overview")
async def overview(access: Access = Depends(get_access), db: AsyncSession = Depends(get_db)):
    rows = [p for p in await _visible_projects(db, access) if not p.archived_at]
    names = await _user_names(db, [p.responsible_user_id for p in rows] + [_uuid_or_none(p.created_by) for p in rows])
    tenant = await _tenant(db, access.tenant_id)
    active, decisions, totals = [], [], {"potential": Decimal("0"), "agreed": Decimal("0"), "realized": Decimal("0")}
    counted = {"potential": 0, "agreed": 0, "realized": 0}
    for p in rows:
        s = await _summary(db, p, access)
        r = await compute_result(db, p, tenant)
        for k in totals:
            if r[k] is not None:
                totals[k] += Decimal(r[k])
                counted[k] += 1
        if _v(p.status) not in ("awarded", "cancelled") or s["decisions"]:
            active.append(_list_row(p, s, names, r))
        for d in s["decisions"]:
            decisions.append({**{k: v for k, v in d.items() if k != "draft"}, "project_id": str(p.id), "project_title": p.title})
    ids = [p.id for p in rows]
    events = []
    if ids:
        q = select(ProjectEvent, Project.title).join(Project, Project.id == ProjectEvent.project_id).where(ProjectEvent.project_id.in_(ids))
        if not access.workspace:
            q = q.where(ProjectEvent.customer_visible == True)  # noqa: E712
        for e, title in (await db.execute(q.order_by(desc(ProjectEvent.created_at)).limit(8))).all():
            events.append({"project_id": str(e.project_id), "project_title": title, "title": e.title, "detail": e.detail,
                           "at": e.created_at.isoformat() if e.created_at else None})
    return {
        "active": active[:8], "active_count": len([a for a in active if a["state"] != "done"]),
        "decisions": decisions[:6], "decisions_count": len(decisions),
        "savings": {k: (str(v) if counted[k] else None) for k, v in totals.items()},
        "savings_counts": counted, "currency": "EUR", "events": events,
    }


@router.get("/decisions")
async def my_decisions(access: Access = Depends(get_access), db: AsyncSession = Depends(get_db)):
    out = []
    for p in await _visible_projects(db, access):
        if p.archived_at:
            continue
        s = await _summary(db, p, access)
        for d in s["decisions"]:
            out.append({**d, "project_id": str(p.id), "project_title": p.title})
    return out


@router.get("/savings")
async def savings(year: Optional[int] = None, access: Access = Depends(get_access), db: AsyncSession = Depends(get_db)):
    """Ergebnis je Vorhaben. Zeitraum = Jahr der Entscheidung (vereinbart)
    bzw. der Abrechnung (realisiert); offene Vorhaben zaehlen nur zum Potenzial."""
    tenant = await _tenant(db, access.tenant_id)
    rows, totals = [], {"potential": None, "agreed": None, "realized": None, "fee": None, "net": None}
    years = set()
    for p in await _visible_projects(db, access):
        if _v(p.status) == "briefing":
            continue
        r = await compute_result(db, p, tenant)
        ref = p.invoiced_at or p.awarded_at or p.created_at
        if ref:
            years.add(ref.year)
        if year and (not ref or ref.year != year):
            continue
        rows.append({"project_id": str(p.id), "title": p.title, "status": _v(p.status),
                     "phase_label": PHASE_LABEL.get(_v(p.status)), "period": ref.isoformat() if ref else None, **r})
        for k in totals:
            if r.get(k) is not None:
                totals[k] = (totals[k] or Decimal("0")) + Decimal(r[k])
    return {"rows": rows, "totals": {k: _s(v) for k, v in totals.items()}, "years": sorted(years, reverse=True),
            "fee_pct": _s(tenant.success_fee_pct) if tenant else None,
            "definitions": {
                "baseline": "Ausgangsbasis: Ihr mitgebrachtes Angebot bzw. das Erstangebot des beauftragten Anbieters.",
                "potential": "Potenzial: in laufenden Vorhaben erreichte oder angestrebte Verbesserung, noch nicht beauftragt.",
                "agreed": "Vereinbart: Ausgangsbasis minus beauftragter Preis.",
                "realized": "Realisiert: Ausgangsbasis minus tatsächlich abgerechneter Betrag (Teilmenge von „vereinbart“).",
                "fee": "Gebühr: vereinbarter Satz auf die realisierte (vorläufig: vereinbarte) Einsparung.",
            }}


# ---------------------------------------------------------------------------
# Neues Vorhaben: Bedarf -> Unterlagen -> Auftrag pruefen (Entwurf jederzeit)
# ---------------------------------------------------------------------------

class ProjectDraft(BaseModel):
    entry_path: Optional[str] = None
    title: Optional[str] = None
    service_type: Optional[str] = None
    description: Optional[str] = None
    must_criteria: Optional[dict] = None
    conditions_text: Optional[str] = None
    budget_target: Optional[str] = None
    budget_ceiling: Optional[str] = None
    currency: Optional[str] = None
    needed_by: Optional[str] = None
    region: Optional[str] = None
    delivery_location: Optional[str] = None
    quantity: Optional[str] = None
    unit: Optional[str] = None
    briefing_input: Optional[str] = None
    open_questions: Optional[list] = None
    notify_milestones: Optional[bool] = None
    seek_alternatives: Optional[bool] = None
    shared_with_tenant: Optional[bool] = None
    incumbent_company: Optional[str] = None
    incumbent_email: Optional[str] = None
    incumbent_contact: Optional[str] = None


def _apply_draft(p: Project, d: ProjectDraft):
    """Uebernimmt nur gesetzte Felder; prueft Formate am Feld, Pflichtfelder erst beim Start."""
    data = d.model_dump(exclude_unset=True)
    if "entry_path" in data and data["entry_path"] in ("new_need", "existing_offer") and _v(p.status) == "briefing":
        p.entry_path = data["entry_path"]
    for k in ("title", "service_type", "description", "conditions_text", "region", "delivery_location", "unit",
              "incumbent_company", "incumbent_contact"):
        if k in data:
            val = (data[k] or "").strip() or None
            setattr(p, k, val[:255] if (val and k in ("title", "service_type", "region", "delivery_location", "unit",
                                                       "incumbent_company", "incumbent_contact")) else val)
    if "incumbent_email" in data:
        em = (data["incumbent_email"] or "").strip().lower() or None
        if em and ("@" not in em or "." not in em.split("@")[-1]):
            raise field_error("incumbent_email", "Bitte eine gültige E-Mail-Adresse angeben.")
        p.incumbent_email = em
    if "must_criteria" in data:
        p.must_criteria_json = {str(k).strip(): str(v).strip() for k, v in (data["must_criteria"] or {}).items() if str(k).strip()}
    for k in ("budget_target", "budget_ceiling", "quantity"):
        if k in data:
            setattr(p, k, _dec(data[k], k))
    if p.budget_target and p.budget_ceiling and p.budget_target > p.budget_ceiling:
        raise field_error("budget_target", "Das Zielbudget darf nicht über der Budget-Obergrenze liegen.")
    if "needed_by" in data:
        if data["needed_by"]:
            try:
                p.needed_by = date.fromisoformat(data["needed_by"][:10])
            except ValueError:
                raise field_error("needed_by", "Bitte ein Datum angeben.")
        else:
            p.needed_by = None
    if data.get("currency"):
        p.currency = data["currency"][:10]
    if "briefing_input" in data:
        p.briefing_input = (data["briefing_input"] or "")[:20000] or None
    if "open_questions" in data:
        p.open_questions_json = [str(q) for q in (data["open_questions"] or [])][:10]
    for k in ("notify_milestones", "seek_alternatives", "shared_with_tenant"):
        if data.get(k) is not None:
            setattr(p, k, bool(data[k]))


@router.post("")
async def create_project(payload: ProjectDraft, access: Access = Depends(get_access), db: AsyncSession = Depends(get_db)):
    """Legt sofort einen Entwurf an -- Eingaben gehen nicht verloren und das
    Vorhaben ist spaeter unter 'Meine Vorhaben' (Status Entwurf) wiederzufinden."""
    p = Project(tenant_id=access.tenant_id, title="Neues Vorhaben (Entwurf)", entry_path="new_need",
                status=ProjectStatus.briefing, customer_email=getattr(access.user, "email", None),
                created_by=str(access.user.id), briefing_source="prompt")
    _apply_draft(p, payload)
    db.add(p)
    await db.flush()
    await add_event(db, p, "created", "Entwurf angelegt", actor=str(access.user.id))
    await db.commit()
    return await _fresh(db, p)


@router.put("/{project_id}")
async def update_project(project_id: str, payload: ProjectDraft, access: Access = Depends(get_access),
                         db: AsyncSession = Depends(get_db)):
    p = await _get_project(project_id, access, db)
    if not _can_edit(p, access):
        raise HTTPException(403, "Sie können dieses Vorhaben nicht bearbeiten.")
    if _v(p.status) in ("awarded", "cancelled"):
        raise HTTPException(400, "Abgeschlossene Vorhaben können nicht mehr geändert werden.")
    if _v(p.status) != "briefing" and payload.entry_path:
        payload.entry_path = None
    _apply_draft(p, payload)
    if p.sourcing_request_id:
        await _ensure_request(db, p, str(access.user.id))
    await db.commit()
    return await _fresh(db, p)


@router.post("/{project_id}/documents")
async def upload_document(project_id: str, kind: str = Form("briefing"), file: UploadFile = File(...),
                          text: Optional[str] = Form(None), access: Access = Depends(get_access),
                          db: AsyncSession = Depends(get_db)):
    """Schritt 'Unterlagen': Briefing-Dokument oder vorhandenes Angebot. Der
    Agent erstellt daraus einen Vorschlag fuer die Zusammenfassung (nichts
    wird erfunden; Fehlendes wird zur offenen Frage)."""
    from services.briefing import draft_briefing
    from services.offer_intake import store_offer_file
    from models_mdc import MDCDocumentType
    p = await _get_project(project_id, access, db)
    if not _can_edit(p, access) or _v(p.status) != "briefing":
        raise HTTPException(400, "Unterlagen können hier nur im Entwurf ergänzt werden.")
    if kind not in ("briefing", "offer"):
        raise field_error("kind", "Unbekannte Art der Unterlage.")
    data = await _read_upload(file, "offer_file" if kind == "offer" else "briefing_file")
    try:
        doc, _ver, doc_text = await store_offer_file(db, p.tenant_id, file.filename or "dokument.pdf", data,
                                                     p.incumbent_company if kind == "offer" else None,
                                                     actor=str(access.user.id), source="kunde:" + kind)
    except ValueError as e:
        raise field_error("offer_file" if kind == "offer" else "briefing_file", str(e))
    if kind == "briefing":
        doc.document_type = MDCDocumentType.other
        doc.usage_purpose = "Briefing eines Vorhabens"
        p.briefing_document_id, p.briefing_source = doc.id, "document"
    else:
        p.offer_document_id, p.briefing_source, p.entry_path = doc.id, "offer", "existing_offer"
    await db.commit()
    if not doc_text:
        raise field_error("offer_file" if kind == "offer" else "briefing_file",
                          "Das Dokument konnte nicht gelesen werden. Bitte den Bedarf als Text beschreiben.")
    combined = "\n\n".join(x for x in [(text or p.briefing_input or "").strip(), doc_text] if x)
    proposal = await asyncio.to_thread(draft_briefing, combined, "offer" if kind == "offer" else "document")
    return {"project": await _fresh(db, p), "proposal": proposal, "document_id": str(doc.id), "file_name": file.filename}


@router.post("/{project_id}/proposal")
async def propose_from_text(project_id: str, access: Access = Depends(get_access), db: AsyncSession = Depends(get_db)):
    """Vorschlag fuer die Zusammenfassung allein aus der Bedarfsbeschreibung."""
    from services.briefing import draft_briefing
    p = await _get_project(project_id, access, db)
    if not _can_edit(p, access):
        raise HTTPException(403, "Sie können dieses Vorhaben nicht bearbeiten.")
    text = (p.briefing_input or p.description or "").strip()
    if len(text) < 15:
        raise field_error("briefing_input", "Bitte den Bedarf in ein, zwei Sätzen beschreiben (Leistung, Umfang, Budget, Termin).")
    return await asyncio.to_thread(draft_briefing, text, "prompt")


class StartPayload(BaseModel):
    confirm_mandate: bool = False


@router.post("/{project_id}/start")
async def start_project(project_id: str, payload: StartPayload, access: Access = Depends(get_access),
                        db: AsyncSession = Depends(get_db)):
    """Auftrag erteilen: Zusammenfassung, Leistungsumfang und
    Verhandlungsmandat sind bestaetigt. Nur mit Entscheidungsbefugnis."""
    require_decider(access)
    p = await _get_project(project_id, access, db)
    if _v(p.status) != "briefing":
        raise HTTPException(400, "Der Auftrag wurde bereits erteilt.")
    missing = _missing_for_start(p)
    if missing:
        field, msg = next(iter(missing.items()))
        raise HTTPException(422, {"field": field, "message": msg, "missing": missing})
    if not payload.confirm_mandate:
        raise field_error("confirm_mandate", "Bitte das Verhandlungsmandat bestätigen.")
    uid = str(access.user.id)
    req = await _ensure_request(db, p, uid)
    req.status, req.approved_by, req.approved_at = SourcingRequestStatus.approved, uid, datetime.utcnow()
    p.mandate_confirmed_at, p.mandate_confirmed_by = datetime.utcnow(), uid

    if p.entry_path == "existing_offer":
        from services.offer_intake import intake_offer
        rfq = await _ensure_rfq(db, p, req, uid)
        cand = SupplierCandidate(
            tenant_id=p.tenant_id, sourcing_request_id=req.id, company_name=p.incumbent_company[:255],
            domain=p.incumbent_email.split("@")[-1], service_match_note=p.service_type or p.title,
            contact_email=p.incumbent_email, contact_name=p.incumbent_contact, source_url="kunde:bestandsangebot",
            retrieved_at=datetime.utcnow(), must_criteria_check_json={}, open_questions_json=[], status=CandidateStatus.qualified,
            nda_assessment_json={"needs_nda": False, "reasoning": "Bestehende Geschaeftsbeziehung, Angebot liegt bereits vor.",
                                 "assessed_at": datetime.utcnow().isoformat()},
            created_by=uid,
        )
        db.add(cand)
        await db.flush()
        p.incumbent_candidate_id = cand.id
        db.add(RFQInvitation(tenant_id=p.tenant_id, rfq_id=rfq.id, supplier_candidate_id=cand.id, sent_at=datetime.utcnow()))
        doc = (await db.execute(select(MDCDocument).where(MDCDocument.id == p.offer_document_id))).scalar_one()
        ver = (await db.execute(select(MDCDocumentVersion).where(MDCDocumentVersion.document_id == doc.id)
                                .order_by(desc(MDCDocumentVersion.version_number)))).scalars().first()
        p.status = ProjectStatus.evaluating
        await add_event(db, p, "started", "Auftrag erteilt",
                        f"Der Agent prüft das Angebot von {cand.company_name}"
                        + (" und holt Vergleichsangebote ein." if p.seek_alternatives else "."), milestone=True, actor=uid)
        await intake_offer(db, rfq, cand, (ver.extracted_text or "") if ver else "", actor=uid, doc=doc, channel="Kunde", preexisting=True)
        if p.seek_alternatives:
            p.status = ProjectStatus.sourcing
    else:
        p.status = ProjectStatus.sourcing
        await add_event(db, p, "started", "Auftrag erteilt",
                        "Der Agent sucht passende Dienstleister und bereitet Anfragen vor. Jede Nachricht an "
                        "Dienstleister wird vor dem Versand freigegeben.", milestone=True, actor=uid)
    await db.commit()
    return await _fresh(db, p)


# ---------------------------------------------------------------------------
# Detail (vier Ansichten) + Arbeitsbereich-Kontext
# ---------------------------------------------------------------------------

@router.get("/{project_id}")
async def get_project(project_id: str, access: Access = Depends(get_access), db: AsyncSession = Depends(get_db)):
    p = await _get_project(project_id, access, db)
    tenant = await _tenant(db, access.tenant_id)
    s = await _summary(db, p, access)
    names = await _user_names(db, [p.responsible_user_id, _uuid_or_none(p.created_by)])
    cand_rows = []
    for c in s["cands"]:
        st = _v(c.status)
        row = {"id": str(c.id), "company_name": c.company_name, "status": st, "status_label": CANDIDATE_LABEL.get(st, st),
               "is_incumbent": p.incumbent_candidate_id == c.id,
               "has_offer": any(o["candidate_id"] == str(c.id) for o in s["offers"])}
        if access.workspace:
            msgs = (await db.execute(select(OutreachMessage).where(OutreachMessage.supplier_candidate_id == c.id)
                                     .order_by(desc(OutreachMessage.occurred_at)))).scalars().all()
            nda = (await db.execute(select(NDA).where(NDA.supplier_candidate_id == c.id))).scalar_one_or_none()
            row.update({"contact_email": c.contact_email, "contact_name": c.contact_name,
                        "source": (c.source_url or "").split("/")[0], "nda_status": _v(nda.status) if nda else None,
                        "open_questions": (c.open_questions_json or [])[-3:],
                        "messages": [{"direction": _v(m.direction), "subject": m.subject, "body": (m.body_text or "")[:1500],
                                      "at": m.occurred_at.isoformat() if m.occurred_at else None} for m in msgs[:10]]})
        cand_rows.append(row)
    q = select(ProjectEvent).where(ProjectEvent.project_id == p.id)
    if not access.workspace:
        q = q.where(ProjectEvent.customer_visible == True)  # noqa: E712
    events = (await db.execute(q.order_by(desc(ProjectEvent.created_at)))).scalars().all()
    return {
        "project": _project_dict(p, names), "state": s["state"], "state_label": STATE_LABEL[s["state"]],
        "stage": _stage(p, s["offers"], s["cands"]), "next": s["next"], "decisions": s["decisions"],
        "offers": s["offers"], "candidates": cand_rows, "result": await compute_result(db, p, tenant),
        "documents": await _documents(db, p),
        "missing": _missing_for_start(p) if _v(p.status) == "briefing" else {},
        "events": [{"kind": e.kind, "title": e.title, "detail": e.detail, "milestone": e.milestone,
                    "internal": not e.customer_visible, "emailed": bool(e.emailed_at),
                    "at": e.created_at.isoformat() if e.created_at else None} for e in events],
        "viewer": {"workspace": access.workspace, "procurement": access.procurement, "can_decide": access.can_decide,
                   "can_approve_messages": access.can_approve_messages, "can_edit": _can_edit(p, access)},
        "workspace": {"drafts": s["drafts"]} if access.workspace else None,
    }


async def _documents(db: AsyncSession, p: Project) -> list[dict]:
    ids, labels = [], {}
    for did, label in ((p.briefing_document_id, "Briefing"), (p.offer_document_id, "Ihr vorhandenes Angebot")):
        if did:
            ids.append(did)
            labels[did] = label
    offers = []
    for rfq in await _project_rfqs(db, p):
        offers += (await db.execute(select(RFQOffer).where(RFQOffer.rfq_id == rfq.id))).scalars().all()
    if offers:
        cand_names = {c.id: c.company_name for c in await _project_candidates(db, p)}
        for d in (await db.execute(select(MDCDocument).where(MDCDocument.rfq_offer_id.in_([o.id for o in offers])))).scalars().all():
            if d.id not in labels:
                o = next(x for x in offers if x.id == d.rfq_offer_id)
                ids.append(d.id)
                labels[d.id] = f"Angebot {cand_names.get(o.supplier_candidate_id, '')}"
    out = []
    for did in ids:
        d = (await db.execute(select(MDCDocument).where(MDCDocument.id == did))).scalar_one_or_none()
        if not d:
            continue
        v = (await db.execute(select(MDCDocumentVersion).where(MDCDocumentVersion.document_id == d.id)
                              .order_by(desc(MDCDocumentVersion.version_number)))).scalars().first()
        out.append({"id": str(d.id), "label": labels[did], "file_name": v.file_name if v else d.title,
                    "uploaded_at": v.created_at.isoformat() if v and v.created_at else None,
                    "downloadable": bool(v and v.file_path and not d.rights_revoked_at)})
    return out


@router.get("/{project_id}/documents/{document_id}")
async def download_document(project_id: str, document_id: str, access: Access = Depends(get_access),
                            db: AsyncSession = Depends(get_db)):
    p = await _get_project(project_id, access, db)
    allowed = {x["id"]: x for x in await _documents(db, p)}
    if document_id not in allowed or not allowed[document_id]["downloadable"]:
        raise HTTPException(404, "Dokument nicht gefunden.")
    v = (await db.execute(select(MDCDocumentVersion).where(MDCDocumentVersion.document_id == uuid.UUID(document_id))
                          .order_by(desc(MDCDocumentVersion.version_number)))).scalars().first()
    if not v or not Path(v.file_path).exists():
        raise HTTPException(404, "Datei nicht mehr vorhanden.")
    return FileResponse(v.file_path, filename=v.file_name)


# ---------------------------------------------------------------------------
# Entscheidungen: Nachrichten freigeben (nur Entwuerfe DIESES Vorhabens)
# ---------------------------------------------------------------------------

class RejectPayload(BaseModel):
    reason: Optional[str] = None


async def _find_draft(db, p, kind, draft_id) -> dict:
    d = next((x for x in await _pending_drafts(db, p) if x["kind"] == kind and x["id"] == draft_id), None)
    if not d:
        raise HTTPException(404, "Entwurf nicht gefunden oder bereits erledigt.")
    return d


@router.post("/{project_id}/drafts/{kind}/{draft_id}/approve")
async def approve_draft(project_id: str, kind: str, draft_id: str, access: Access = Depends(get_access),
                        db: AsyncSession = Depends(get_db)):
    p = await _get_project(project_id, access, db)
    d = await _find_draft(db, p, kind, draft_id)
    if d.get("workspace_only"):
        raise HTTPException(403, "Diese Prüfung erfolgt im Arbeitsbereich.")
    require_message_approver(access)
    from routers import sourcing, rfq_contracts, negotiation
    u, m = access.user, access.membership
    if kind == "outreach":
        return await sourcing.approve_outreach(candidate_id=d["scope_id"], action_id=draft_id, user=u, membership=m, db=db)
    if kind == "nda":
        return await sourcing.approve_send_nda(nda_id=draft_id, user=u, membership=m, db=db)
    if kind == "rfq":
        return await rfq_contracts.approve_rfq_action(rfq_id=d["scope_id"], action_id=draft_id, user=u, membership=m, db=db)
    if kind == "negotiation":
        return await negotiation.approve_action(case_id=d["scope_id"], action_id=draft_id, payload=negotiation.ApprovalRequest(),
                                                user=u, membership=m, db=db)
    raise HTTPException(404, "Unbekannter Entwurf.")


@router.post("/{project_id}/drafts/{kind}/{draft_id}/reject")
async def reject_draft(project_id: str, kind: str, draft_id: str, payload: RejectPayload,
                       access: Access = Depends(get_access), db: AsyncSession = Depends(get_db)):
    p = await _get_project(project_id, access, db)
    d = await _find_draft(db, p, kind, draft_id)
    if d.get("workspace_only") and not access.procurement:
        raise HTTPException(403, "Diese Prüfung erfolgt im Arbeitsbereich.")
    require_message_approver(access)
    from routers import sourcing, rfq_contracts, negotiation
    u, m = access.user, access.membership
    if kind == "outreach":
        return await sourcing.reject_outreach(candidate_id=d["scope_id"], action_id=draft_id, membership=m, db=db)
    if kind == "nda":
        return await sourcing.reject_nda(nda_id=draft_id, user=u, membership=m, db=db)
    if kind == "rfq":
        return await rfq_contracts.reject_rfq_action(rfq_id=d["scope_id"], action_id=draft_id, membership=m, db=db)
    if kind == "negotiation":
        return await negotiation.reject_action(case_id=d["scope_id"], action_id=draft_id,
                                               payload=negotiation.RejectRequest(reason=payload.reason or "im Vorhaben verworfen"),
                                               user=u, membership=m, db=db)
    raise HTTPException(404, "Unbekannter Entwurf.")


# ---------------------------------------------------------------------------
# Dienstleister auswaehlen (interne Vorschlaege oder manuell)
# ---------------------------------------------------------------------------

def _require_selector(access: Access):
    if not (access.can_decide or access.procurement):
        raise HTTPException(403, "Dienstleister auswählen dürfen entscheidungsbefugte Nutzer oder Procurement-Experten.")


@router.get("/{project_id}/suggestions")
async def suggestions(project_id: str, access: Access = Depends(get_access), db: AsyncSession = Depends(get_db)):
    """Interne Recherche: Lieferantenstamm, frueher angefragte Dienstleister,
    Dienstleister aus dem Data Center. Rangfolge per lokaler semantischer
    Aehnlichkeit zum Briefing -- ein Vorschlag, keine Auswahl."""
    _require_selector(access)
    p = await _get_project(project_id, access, db)
    tid = access.tenant_id
    existing_emails, existing_names = set(), set()
    for c in await _project_candidates(db, p):
        existing_emails.add((c.contact_email or "").lower())
        existing_names.add((c.company_name or "").strip().lower())

    pool: dict = {}

    def put(key, entry):
        if key in pool:
            pool[key]["reasons"] += entry["reasons"]
            pool[key]["profile"] += " " + entry["profile"]
            for f in ("contact_email", "contact_name"):
                pool[key][f] = pool[key][f] or entry[f]
        else:
            pool[key] = entry

    for s in (await db.execute(select(SupplierV2).where(SupplierV2.tenant_id == tid))).scalars().all():
        key = (s.email or s.name).strip().lower()
        put(key, {"source": "supplier", "ref_id": str(s.id), "company_name": s.name, "contact_email": s.email,
                  "contact_name": s.contact_name, "reasons": [f"Im Lieferantenstamm{f' ({s.category})' if s.category else ''}"],
                  "profile": " ".join(x for x in [s.name, s.category or "", s.capacity_notes or "", s.country or ""] if x)})
    prev = (await db.execute(select(SupplierCandidate, SourcingRequest).join(
        SourcingRequest, SourcingRequest.id == SupplierCandidate.sourcing_request_id).where(
        SupplierCandidate.tenant_id == tid, SupplierCandidate.contact_email.isnot(None)))).all()
    for c, r in prev:
        if p.sourcing_request_id and c.sourcing_request_id == p.sourcing_request_id:
            continue
        key = c.contact_email.strip().lower()
        st = _v(c.status)
        put(key, {"source": "candidate", "ref_id": str(c.id), "company_name": c.company_name, "contact_email": c.contact_email,
                  "contact_name": c.contact_name, "reasons": [f"Früher angefragt: „{r.title[:60]}“ ({CANDIDATE_LABEL.get(st, st)})"],
                  "profile": " ".join(x for x in [c.company_name, c.service_match_note or "", r.title, r.bedarf_text[:300]] if x)})
    docs = (await db.execute(select(MDCSupplier, MDCDocument).join(MDCDocument, MDCDocument.supplier_id == MDCSupplier.id)
                             .where(MDCSupplier.tenant_id == tid))).all()
    mdc_titles: dict = {}
    for s, d in docs:
        mdc_titles.setdefault(s.id, (s, []))[1].append(d.title or "")
    for sid, (s, titles) in mdc_titles.items():
        match = next((k for k, v in pool.items() if v["company_name"].strip().lower() == s.name.strip().lower()), None)
        put(match or f"mdc:{sid}", {"source": "mdc", "ref_id": str(sid), "company_name": s.name, "contact_email": None,
                                    "contact_name": None, "reasons": [f"Im Data Center: {len(titles)} Dokument(e)"],
                                    "profile": s.name + " " + " ".join(titles[:5])})

    items = [e for e in pool.values() if (e["contact_email"] or "").lower() not in existing_emails
             and e["company_name"].strip().lower() not in existing_names]
    if not items:
        return {"items": [], "note": "Keine weiteren Dienstleister im Bestand gefunden. Sie können Dienstleister manuell hinzufügen."}
    query = " ".join(x for x in [p.title, p.service_type or "", p.description or "",
                                 " ".join(p.must_criteria_json.values()) if p.must_criteria_json else ""] if x)
    try:
        from services.mdc_embeddings import embed_texts
        vecs = await asyncio.to_thread(embed_texts, [query] + [e["profile"][:2000] for e in items])
        for e, v in zip(items, vecs[1:]):
            e["score"] = round(sum(a * b for a, b in zip(vecs[0], v)), 3)
    except Exception:
        logger.exception("Vorschlags-Ranking ohne Embeddings")
        for e in items:
            e["score"] = None
    items.sort(key=lambda e: (e["score"] is None, -(e["score"] or 0)))
    for e in items:
        e.pop("profile", None)
        e["contactable"] = bool(e["contact_email"])
    return {"items": items[:20], "note": "Sortiert nach inhaltlicher Nähe zu Ihrem Bedarf. Externe Recherche folgt in einer späteren Ausbaustufe."}


class CandidateSelection(BaseModel):
    source: str
    ref_id: Optional[str] = None
    company_name: str
    contact_email: Optional[str] = None
    contact_name: Optional[str] = None
    note: Optional[str] = None
    website: Optional[str] = None


class SelectPayload(BaseModel):
    items: list[CandidateSelection]


@router.post("/{project_id}/candidates")
async def select_candidates(project_id: str, payload: SelectPayload, access: Access = Depends(get_access),
                            db: AsyncSession = Depends(get_db)):
    _require_selector(access)
    p = await _get_project(project_id, access, db)
    if _v(p.status) in ("briefing", "awarded", "cancelled"):
        raise HTTPException(400, "Dienstleister können erst nach Auftragserteilung und vor der Entscheidung ausgewählt werden.")
    uid = str(access.user.id)
    req = await _ensure_request(db, p, uid)
    existing = await _project_candidates(db, p)
    if len([c for c in existing if _v(c.status) != "rejected"]) + len(payload.items) > req.max_candidates_total:
        raise HTTPException(409, f"Höchstens {req.max_candidates_total} Dienstleister je Vorhaben.")
    seen = {(c.contact_email or "").lower() for c in existing if c.contact_email}
    created, skipped = [], []
    for it in payload.items:
        email = (it.contact_email or "").strip().lower() or None
        if not it.company_name.strip():
            raise field_error("company_name", "Bitte den Firmennamen angeben.")
        if email and ("@" not in email or "." not in email.split("@")[-1]):
            if it.source == "manual":
                raise field_error("contact_email", "Bitte eine gültige E-Mail-Adresse angeben.")
            skipped.append({"company_name": it.company_name, "reason": "E-Mail-Adresse ungültig"})
            continue
        if email and email in seen:
            skipped.append({"company_name": it.company_name, "reason": "bereits im Vorhaben"})
            continue
        src = {"supplier": "intern:lieferantenstamm", "candidate": "intern:fruehere-anfrage", "mdc": "intern:data-center",
               "manual": it.website or "manuell:kunde"}.get(it.source, "manuell:kunde")
        crit = {k: "unknown" for k in (p.must_criteria_json or {})}
        cand = SupplierCandidate(
            tenant_id=p.tenant_id, sourcing_request_id=req.id, company_name=it.company_name.strip()[:255],
            domain=(email.split("@")[-1] if email else None), service_match_note=(it.note or p.service_type or p.title)[:500],
            contact_email=email, contact_name=it.contact_name, source_url=f"{src}/{it.ref_id}" if it.ref_id else src,
            retrieved_at=datetime.utcnow(), existing_supplier_id=_uuid_or_none(it.ref_id) if it.source == "supplier" else None,
            must_criteria_check_json=crit, open_questions_json=[f"Bitte bestaetigen Sie: {k}" for k in crit],
            status=CandidateStatus.shortlisted, created_by=uid,
        )
        db.add(cand)
        await db.flush()
        if email:
            seen.add(email)
            subject, body = _first_contact(p, cand)
            db.add(OutreachAction(tenant_id=p.tenant_id, supplier_candidate_id=cand.id, kind="first_contact",
                                  reminder_number=0, recipient_email=email, rendered_subject=subject, rendered_body=body,
                                  status=OutreachActionStatus.draft, created_by=uid))
        created.append({"id": str(cand.id), "company_name": cand.company_name, "draft": bool(email)})
    if created:
        await add_event(db, p, "candidates_selected", f"{len(created)} Dienstleister ausgewählt",
                        ", ".join(c["company_name"] for c in created) + ". Die Erstanfragen liegen zur Freigabe bereit.", actor=uid)
    await db.commit()
    return {"created": created, "skipped": skipped}


# ---------------------------------------------------------------------------
# Angebote: Upload, Pruefung, Verhandlung
# ---------------------------------------------------------------------------

async def _get_offer(db: AsyncSession, p: Project, offer_id: str) -> RFQOffer:
    oid = _uuid_or_none(offer_id)
    o = (await db.execute(select(RFQOffer).where(RFQOffer.id == oid))).scalar_one_or_none() if oid else None
    rfq_ids = [r.id for r in await _project_rfqs(db, p)]
    if not o or o.tenant_id != p.tenant_id or o.rfq_id not in rfq_ids:
        raise HTTPException(404, "Angebot gehört nicht zu diesem Vorhaben.")
    return o


def _require_operator(access: Access):
    if not (access.procurement or access.can_decide):
        raise HTTPException(403, "Dafür fehlt Ihnen die Berechtigung.")


@router.post("/{project_id}/offers/upload")
async def upload_offer(project_id: str, candidate_id: str = Form(...), file: UploadFile = File(...),
                       access: Access = Depends(get_access), db: AsyncSession = Depends(get_db)):
    """Angebot, das auf anderem Weg kam (Post, Portal), einem Dienstleister zuordnen."""
    from services.offer_intake import store_offer_file, intake_offer
    _require_operator(access)
    p = await _get_project(project_id, access, db)
    cand = (await db.execute(select(SupplierCandidate).where(SupplierCandidate.id == _uuid_or_none(candidate_id)))).scalar_one_or_none()
    if not cand or cand.tenant_id != p.tenant_id or cand.sourcing_request_id != p.sourcing_request_id:
        raise field_error("candidate_id", "Dieser Dienstleister gehört nicht zum Vorhaben.")
    data = await _read_upload(file)
    uid = str(access.user.id)
    req = await _ensure_request(db, p, uid)
    rfq = await _ensure_rfq(db, p, req, uid)
    try:
        doc, _ver, text = await store_offer_file(db, p.tenant_id, file.filename or "angebot.pdf", data, cand.company_name,
                                                 category_id=p.category_id, actor=uid, source="upload")
    except ValueError as e:
        raise field_error("file", str(e))
    if not text:
        raise field_error("file", "Die Datei konnte nicht gelesen werden.")
    offer, review = await intake_offer(db, rfq, cand, text, actor=uid, doc=doc, channel="Upload", draft_receipt=False)
    await db.commit()
    return {"offer_id": str(offer.id), "review": review.status}


@router.post("/{project_id}/offers/{offer_id}/review")
async def rerun_review(project_id: str, offer_id: str, access: Access = Depends(get_access), db: AsyncSession = Depends(get_db)):
    from services.offer_intake import run_review
    _require_operator(access)
    p = await _get_project(project_id, access, db)
    offer = await _get_offer(db, p, offer_id)
    doc = (await db.execute(select(MDCDocument).where(MDCDocument.rfq_offer_id == offer.id))).scalars().first()
    text = ""
    if doc:
        ver = (await db.execute(select(MDCDocumentVersion).where(MDCDocumentVersion.document_id == doc.id)
                                .order_by(desc(MDCDocumentVersion.version_number)))).scalars().first()
        text = (ver.extracted_text or "") if ver else ""
    else:
        prev = (await db.execute(select(OfferReview).where(OfferReview.offer_id == offer.id).order_by(desc(OfferReview.created_at)))).scalars().first()
        text = prev.offer_text_excerpt if prev else ""
    rv = await run_review(db, offer, p, text, actor=str(access.user.id))
    await db.commit()
    return {"status": rv.status, "findings": rv.findings_json}


class NegotiatePayload(BaseModel):
    round1_price: Optional[str] = None
    round2_price: Optional[str] = None
    usage_rights_text: Optional[str] = None


@router.post("/{project_id}/offers/{offer_id}/negotiate")
async def negotiate_offer(project_id: str, offer_id: str, payload: NegotiatePayload, access: Access = Depends(get_access),
                          db: AsyncSession = Depends(get_db)):
    """Verhandlung VOR der Entscheidung ueber die bestehende Engine. Obergrenze
    ist der Angebotspreis -- es wird nie mehr geboten als angeboten."""
    from routers.cases import _transition as case_transition
    from routers.negotiation import draft_action
    _require_operator(access)
    p = await _get_project(project_id, access, db)
    offer = await _get_offer(db, p, offer_id)
    if _v(offer.status) != "submitted":
        raise HTTPException(400, "Nur das aktuelle Angebot kann verhandelt werden.")
    if str(offer.id) in (p.negotiations_json or {}):
        raise HTTPException(400, "Für dieses Angebot läuft bereits eine Verhandlung.")
    total = offer_total(offer, p.quantity)
    if total is None:
        raise HTTPException(400, "Für dieses Angebot wurde kein Preis erkannt.")
    cand = (await db.execute(select(SupplierCandidate).where(SupplierCandidate.id == offer.supplier_candidate_id))).scalar_one()
    if not cand.contact_email:
        raise HTTPException(400, "Keine Kontaktadresse des Dienstleisters hinterlegt.")
    q2 = Decimal("0.01")
    if payload.round1_price:
        r1 = _dec(payload.round1_price, "round1_price")
    elif p.budget_target and p.budget_target < total:
        r1 = max(Decimal(p.budget_target), total * Decimal("0.85"))
    else:
        r1 = total * Decimal("0.90")
    r1 = r1.quantize(q2)
    r2 = (_dec(payload.round2_price, "round2_price") if payload.round2_price else (r1 + total) / 2).quantize(q2)
    if not (Decimal("0") < r1 <= total):
        raise field_error("round1_price", f"Der erste Vorschlag muss über 0 und höchstens beim Angebotspreis ({total}) liegen.")
    if not (r1 <= r2 <= total):
        raise field_error("round2_price", f"Der zweite Vorschlag muss zwischen erstem Vorschlag und Angebotspreis ({total}) liegen.")
    uid = str(access.user.id)
    case = Case(tenant_id=p.tenant_id, title=f"{p.title} — Verhandlung {cand.company_name}"[:255],
                category="project_negotiation", status=CaseStatus.RECEIVED, case_version=1)
    db.add(case)
    await db.flush()
    db.add(CaseEvent(case_id=case.id, tenant_id=case.tenant_id, from_status=None, to_status=CaseStatus.RECEIVED.value,
                     actor=uid, reason=f"Verhandlung im Vorhaben {p.id} (Angebot {offer.id}, vor Entscheidung)."))
    db.add(NegotiationStrategy(
        tenant_id=p.tenant_id, case_id=case.id, starting_price_net=total, round1_price_net=r1, round2_price_net=r2,
        price_cap_net=total, currency=offer.currency or p.currency or "EUR", scope_text=(p.description or p.title)[:1000],
        usage_rights_text=payload.usage_rights_text or p.conditions_text or "wie im Angebot beschrieben, unveraendert",
        delivery_date=offer.delivery_date or (p.needed_by.strftime("%d.%m.%Y") if p.needed_by else "wie angeboten"),
        max_rounds=2, supplier_email=cand.contact_email.lower(), created_by=uid,
    ))
    await case_transition(db, case, CaseStatus.READY_TO_DRAFT, actor=uid, reason="Verhandlungsrahmen bestaetigt.")
    p.negotiations_json = {**(p.negotiations_json or {}), str(offer.id): str(case.id)}
    if _v(p.status) in ("sourcing", "collecting_offers"):
        p.status = ProjectStatus.evaluating
    await add_event(db, p, "negotiation_started", f"Verhandlung mit {cand.company_name} vorbereitet",
                    f"Erster Vorschlag {r1} {offer.currency}, zweiter Vorschlag {r2} {offer.currency} (Angebot {total}).", actor=uid)
    await db.commit()
    draft = await draft_action(case_id=str(case.id), user=access.user, membership=access.membership, db=db)
    return {"case_id": str(case.id), "round1": str(r1), "round2": str(r2), "draft": draft}


@router.post("/{project_id}/offers/{offer_id}/next-round")
async def next_round(project_id: str, offer_id: str, access: Access = Depends(get_access), db: AsyncSession = Depends(get_db)):
    from routers.negotiation import draft_action
    _require_operator(access)
    p = await _get_project(project_id, access, db)
    case_id = (p.negotiations_json or {}).get(offer_id)
    if not case_id:
        raise HTTPException(404, "Keine Verhandlung zu diesem Angebot.")
    return await draft_action(case_id=case_id, user=access.user, membership=access.membership, db=db)


class NegotiatedResult(BaseModel):
    total_price: str
    note: Optional[str] = None


@router.post("/{project_id}/offers/{offer_id}/record-result")
async def record_result(project_id: str, offer_id: str, payload: NegotiatedResult, access: Access = Depends(get_access),
                        db: AsyncSession = Depends(get_db)):
    """Verhandlungsergebnis als neue Angebotsversion (alte bleibt als Historie)."""
    _require_operator(access)
    p = await _get_project(project_id, access, db)
    offer = await _get_offer(db, p, offer_id)
    if _v(offer.status) != "submitted":
        raise HTTPException(400, "Nur das aktuelle Angebot kann aktualisiert werden.")
    total = _dec(payload.total_price, "total_price")
    if total is None or total <= 0:
        raise field_error("total_price", "Bitte den verhandelten Gesamtpreis angeben.")
    old_total = offer_total(offer, p.quantity)
    qty = offer.quantity if offer.quantity is not None else (p.quantity if p.quantity is not None else Decimal("1"))
    extra = Decimal(offer.freight_cost or 0) + Decimal(offer.other_costs or 0)
    new = RFQOffer(
        tenant_id=offer.tenant_id, rfq_id=offer.rfq_id, supplier_candidate_id=offer.supplier_candidate_id,
        version=offer.version + 1, unit_price=((total - extra) / Decimal(qty)).quantize(Decimal("0.0001")), quantity=offer.quantity,
        freight_cost=offer.freight_cost, other_costs=offer.other_costs, currency=offer.currency, delivery_date=offer.delivery_date,
        payment_terms=offer.payment_terms, offer_validity_until=offer.offer_validity_until, scope_note=offer.scope_note,
        spec_confirmed=offer.spec_confirmed, comparability_flag=offer.comparability_flag,
        comparability_note=offer.comparability_note, raw_extracted_json={"source": "verhandlungsergebnis", "note": payload.note},
        created_by=str(access.user.id),
    )
    db.add(new)
    await db.flush()
    offer.status, offer.superseded_by_id = OfferStatus.superseded, new.id
    if str(offer.id) in (p.negotiations_json or {}):
        p.negotiations_json = {**p.negotiations_json, str(new.id): p.negotiations_json[str(offer.id)]}
    cand = (await db.execute(select(SupplierCandidate).where(SupplierCandidate.id == offer.supplier_candidate_id))).scalar_one()
    diff = (old_total - total) if old_total is not None else None
    await add_event(db, p, "negotiation_result", f"Verhandlungsergebnis {cand.company_name}: {total} {offer.currency}",
                    f"{diff} {offer.currency} unter dem bisherigen Angebot." if diff is not None and diff > 0 else None,
                    milestone=True, actor=str(access.user.id))
    await db.commit()
    return {"offer_id": str(new.id), "version": new.version}


# ---------------------------------------------------------------------------
# Entscheidung, Abrechnung, Archiv, Zustaendigkeit
# ---------------------------------------------------------------------------

class AwardPayload(BaseModel):
    offer_id: str
    note: Optional[str] = None


@router.post("/{project_id}/award")
async def award(project_id: str, payload: AwardPayload, access: Access = Depends(get_access), db: AsyncSession = Depends(get_db)):
    """Die Entscheidung des Kunden (nur mit Entscheidungsbefugnis). Erzeugt
    Zu-/Absage-ENTWUERFE -- kein Vertragsschluss durch das System."""
    require_decider(access)
    p = await _get_project(project_id, access, db)
    if _v(p.status) in ("awarded", "cancelled", "briefing"):
        raise HTTPException(400, "In diesem Status ist keine Beauftragung möglich.")
    offer = await _get_offer(db, p, payload.offer_id)
    if _v(offer.status) != "submitted":
        raise HTTPException(400, "Bitte die aktuelle Angebotsversion wählen.")
    uid = str(access.user.id)
    rfq = (await db.execute(select(RFQ).where(RFQ.id == offer.rfq_id))).scalar_one()
    winner = (await db.execute(select(SupplierCandidate).where(SupplierCandidate.id == offer.supplier_candidate_id))).scalar_one()
    p.status, p.awarded_offer_id, p.awarded_at, p.awarded_by = ProjectStatus.awarded, offer.id, datetime.utcnow(), uid
    p.decision_note = payload.note
    rfq.status, rfq.awarded_offer_id, rfq.awarded_by, rfq.awarded_at = RFQStatus.awarded, offer.id, uid, datetime.utcnow()
    total = offer_total(offer, p.quantity)
    if winner.contact_email:
        db.add(RFQAction(
            tenant_id=p.tenant_id, rfq_id=rfq.id, supplier_candidate_id=winner.id, kind="award_notice",
            recipient_email=winner.contact_email, rendered_subject=f"Zusage: {p.title} / {str(winner.id)[:8]}",
            rendered_body=(f"Guten Tag,\n\nvielen Dank fuer Ihr Angebot. Wir freuen uns, Ihnen mitzuteilen, dass sich der "
                           f"Auftraggeber fuer Ihr Angebot entschieden hat (Gesamtpreis {total} {offer.currency} netto, "
                           "wie zuletzt angeboten).\n\nDie Beauftragung erfolgt gesondert durch den Auftraggeber; mit dieser "
                           "Nachricht kommt noch kein Vertrag zustande. Wir melden uns zu den naechsten Schritten.\n\n"
                           "Freundliche Gruesse,\nNegotiateX – KI-gestuetzte Beschaffungskoordination.\nTestlauf, keine Beauftragung."),
            status=RFQActionStatus.draft, created_by=uid))
    declined = 0
    for c in await _project_candidates(db, p):
        if c.id == winner.id or not c.contact_email:
            continue
        has_offer = (await db.execute(select(RFQOffer).where(RFQOffer.supplier_candidate_id == c.id))).scalars().first()
        if not has_offer:
            continue
        db.add(RFQAction(
            tenant_id=p.tenant_id, rfq_id=has_offer.rfq_id, supplier_candidate_id=c.id, kind="decline_notice",
            recipient_email=c.contact_email, rendered_subject=f"Ihre Angebotsabgabe: {p.title} / {str(c.id)[:8]}",
            rendered_body=("Guten Tag,\n\nvielen Dank fuer Ihr Angebot und die Zeit, die Sie investiert haben. Der Auftraggeber "
                           "hat sich in diesem Fall fuer ein anderes Angebot entschieden. Wir wuerden uns freuen, Sie bei "
                           "kuenftigen Anfragen wieder zu beruecksichtigen.\n\nFreundliche Gruesse,\nNegotiateX – "
                           "KI-gestuetzte Beschaffungskoordination.\nTestlauf, keine Beauftragung."),
            status=RFQActionStatus.draft, created_by=uid))
        declined += 1
    await db.flush()
    p.result_json = await compute_result(db, p, await _tenant(db, p.tenant_id))  # Snapshot zum Entscheidungszeitpunkt
    await add_event(db, p, "awarded", f"Entscheidung: {winner.company_name}",
                    f"Gesamtpreis {total} {offer.currency}. Zusage und {declined} Absage(n) liegen zur Freigabe bereit.",
                    milestone=True, actor=uid)
    await db.commit()
    return await _fresh(db, p)


class InvoicePayload(BaseModel):
    amount: str


@router.post("/{project_id}/invoice")
async def record_invoice(project_id: str, payload: InvoicePayload, access: Access = Depends(get_access),
                         db: AsyncSession = Depends(get_db)):
    """Tatsaechlich abgerechneter Betrag -> realisierte Einsparung."""
    _require_operator(access)
    p = await _get_project(project_id, access, db)
    if _v(p.status) != "awarded":
        raise HTTPException(400, "Den abgerechneten Betrag können Sie nach der Beauftragung erfassen.")
    amount = _dec(payload.amount, "amount")
    if not amount:
        raise field_error("amount", "Bitte den abgerechneten Nettobetrag angeben.")
    p.invoiced_amount, p.invoiced_at, p.invoiced_by = amount, datetime.utcnow(), str(access.user.id)
    await add_event(db, p, "invoiced", f"Abgerechneter Betrag erfasst: {amount} {p.currency}", actor=str(access.user.id))
    await db.commit()
    return await _fresh(db, p)


@router.post("/{project_id}/archive")
async def archive(project_id: str, restore: bool = False, access: Access = Depends(get_access), db: AsyncSession = Depends(get_db)):
    p = await _get_project(project_id, access, db)
    if not _can_edit(p, access):
        raise HTTPException(403, "Sie können dieses Vorhaben nicht archivieren.")
    if not restore and _v(p.status) not in ("awarded", "cancelled", "briefing"):
        raise HTTPException(400, "Laufende Vorhaben können nicht archiviert werden – bitte zuerst abschließen oder abbrechen.")
    p.archived_at = None if restore else datetime.utcnow()
    await db.commit()
    return await _fresh(db, p)


@router.post("/{project_id}/cancel")
async def cancel(project_id: str, access: Access = Depends(get_access), db: AsyncSession = Depends(get_db)):
    p = await _get_project(project_id, access, db)
    if not (_can_edit(p, access) and (access.can_decide or access.procurement or _v(p.status) == "briefing")):
        raise HTTPException(403, "Sie können dieses Vorhaben nicht abbrechen.")
    if _v(p.status) == "awarded":
        raise HTTPException(400, "Bereits beauftragt.")
    p.status = ProjectStatus.cancelled
    if p.sourcing_request_id:
        req = (await db.execute(select(SourcingRequest).where(SourcingRequest.id == p.sourcing_request_id))).scalar_one_or_none()
        if req:
            req.status = SourcingRequestStatus.closed
    for d in await _pending_drafts(db, p):
        model = {"outreach": OutreachAction, "rfq": RFQAction, "negotiation": NegotiationAction}.get(d["kind"])
        if model is None:
            continue
        a = (await db.execute(select(model).where(model.id == uuid.UUID(d["id"])))).scalar_one_or_none()
        if a is not None:
            a.status = type(a.status)("rejected")
    await add_event(db, p, "cancelled", "Vorhaben abgebrochen", "Offene Entwürfe wurden verworfen.", milestone=True, actor=str(access.user.id))
    await db.commit()
    return await _fresh(db, p)


class ResponsiblePayload(BaseModel):
    user_id: Optional[str] = None


@router.put("/{project_id}/responsible")
async def set_responsible(project_id: str, payload: ResponsiblePayload, access: Access = Depends(get_access),
                          db: AsyncSession = Depends(get_db)):
    if not access.workspace:
        raise HTTPException(403, "Die Zuständigkeit wird im Arbeitsbereich festgelegt.")
    p = await _get_project(project_id, access, db)
    uid = _uuid_or_none(payload.user_id)
    if payload.user_id and not uid:
        raise field_error("user_id", "Unbekannter Nutzer.")
    if uid:
        m = (await db.execute(select(Membership).where(Membership.user_id == uid, Membership.tenant_id == p.tenant_id))).scalar_one_or_none()
        from access import roles_of
        if not m or "procurement" not in roles_of(m):
            raise field_error("user_id", "Verantwortlich kann nur ein Procurement-Experte dieses Mandanten sein.")
    p.responsible_user_id = uid
    names = await _user_names(db, [uid])
    await add_event(db, p, "responsible", f"Verantwortlich: {names.get(uid, 'nicht zugewiesen')}", actor=str(access.user.id))
    await db.commit()
    return await _fresh(db, p)
