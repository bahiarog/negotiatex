"""
Uebergreifende Uebersicht fuer den Menschen im Freigabe-Loop (alle Teile
A/B/C): zeigt an einem Ort, was der Agent vorbereitet hat und auf eine
menschliche Entscheidung wartet, plus die juengste Aktivitaet.

Reiner Lesebetrieb -- kein Endpunkt hier loest selbst etwas aus. Jeder
Eintrag verlinkt auf den bestehenden, bereits freigabegebundenen Endpunkt im
jeweiligen Modul (negotiation.py / sourcing.py / rfq_contracts.py). Diese
Datei fuehrt also keine neue Aktionsebene ein, sondern aggregiert nur, was
dort ohnehin schon den Status "draft"/"wartet auf Pruefung" hat.
"""
import logging

from fastapi import APIRouter, Depends
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from deps import get_current_membership
from models_v2 import Case, CaseEvent
from models_negotiation import NegotiationAction, NegotiationActionStatus, NegotiationException
from models_sourcing import SupplierCandidate, OutreachAction, OutreachActionStatus, NDA, NDAStatus, NDAEvent
from models_contracts import RFQAction, RFQActionStatus, ContractAction, ContractActionStatus
from models_mdc import MDCLineItem, MDCReviewStatus, MDCDocumentVersion, MDCDocument

logger = logging.getLogger(__name__)
router = APIRouter()

NDA_NEXT_STEP = {
    "draft": "Entwurf wartet auf Freigabe zum Versand",
    "returned": "Ruecklauf eingegangen -- Pruefung (/verify) noetig",
    "review_required": "Abweichung zum Original erkannt -- rechtliche Pruefung noetig",
    "verified": "Menschlich geprueft -- wartet auf finale Freigabe (/approve)",
}

OUTREACH_LABELS = {"first_contact": "Erstkontakt", "reminder": "Erinnerung", "stammdaten_invite": "Stammdaten-Einladung"}
RFQ_LABELS = {"invite": "RFQ-Einladung", "clarification_broadcast": "Klarstellung an Bieter", "receipt_confirmation": "Eingangsbestaetigung"}
CONTRACT_LABELS = {"send_draft": "Vertragsentwurf", "propose_clause": "Klausel-Gegenvorschlag"}


def _item(category, label, subject, detail, link, created_at):
    return {
        "category": category, "label": label, "subject": subject, "detail": detail,
        "link": link, "created_at": created_at.isoformat() if created_at else None,
    }


@router.get("/overview")
async def agent_overview(membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    tid = membership.tenant_id
    pending = []

    # Teil A + B8 (Angebots-Verhandlung ueber dieselbe Engine): Preisvorschlaege/Erinnerungen
    r = await db.execute(select(NegotiationAction).where(
        NegotiationAction.tenant_id == tid,
        NegotiationAction.status.in_([NegotiationActionStatus.draft, NegotiationActionStatus.pending_approval]),
    ).order_by(desc(NegotiationAction.created_at)))
    for a in r.scalars().all():
        case = (await db.execute(select(Case).where(Case.id == a.case_id))).scalar_one_or_none()
        pending.append(_item(
            "negotiation", "Verhandlung", case.title if case else "Fall",
            f"{a.action_type.value}, Runde {a.round_number}, an {a.recipient_email}",
            f"/cases-dashboard#case/{a.case_id}", a.created_at,
        ))

    # A5-Ausnahmen -- blockieren den naechsten automatischen Schritt, bis ein Mensch sie aufloest
    r = await db.execute(select(NegotiationException).where(
        NegotiationException.tenant_id == tid, NegotiationException.resolved == False,  # noqa: E712
    ).order_by(desc(NegotiationException.detected_at)))
    for ex in r.scalars().all():
        case = (await db.execute(select(Case).where(Case.id == ex.case_id))).scalar_one_or_none()
        pending.append(_item(
            "exception", "Ausnahme (Pruefung noetig)", case.title if case else "Fall",
            f"{ex.exception_type.value}: {(ex.detail_text or '')[:180]}",
            f"/cases-dashboard#case/{ex.case_id}", ex.detected_at,
        ))

    # Teil B4: Erstkontakt / Erinnerung / Stammdaten-Einladung
    r = await db.execute(select(OutreachAction).where(
        OutreachAction.tenant_id == tid, OutreachAction.status == OutreachActionStatus.draft,
    ).order_by(desc(OutreachAction.created_at)))
    for a in r.scalars().all():
        cand = (await db.execute(select(SupplierCandidate).where(SupplierCandidate.id == a.supplier_candidate_id))).scalar_one_or_none()
        pending.append(_item(
            "outreach", OUTREACH_LABELS.get(a.kind, a.kind), cand.company_name if cand else "Kandidat",
            f"An {a.recipient_email}: {(a.rendered_subject or '')[:120]}",
            f"/sourcing-dashboard#candidate/{a.supplier_candidate_id}", a.created_at,
        ))

    # Teil B6: NDA -- je nach Status ein anderer naechster menschlicher Schritt
    r = await db.execute(select(NDA).where(
        NDA.tenant_id == tid, NDA.status.in_([NDAStatus.draft, NDAStatus.returned, NDAStatus.review_required, NDAStatus.verified]),
    ).order_by(desc(NDA.updated_at)))
    for n in r.scalars().all():
        cand = (await db.execute(select(SupplierCandidate).where(SupplierCandidate.id == n.supplier_candidate_id))).scalar_one_or_none()
        pending.append(_item(
            "nda", "NDA", cand.company_name if cand else (n.party_b_name or "Kandidat"),
            NDA_NEXT_STEP.get(n.status.value, n.status.value),
            f"/sourcing-dashboard#candidate/{n.supplier_candidate_id}", n.updated_at,
        ))

    # Teil B7: RFQ-Mails (Einladung, Klarstellung, Eingangsbestaetigung)
    r = await db.execute(select(RFQAction).where(
        RFQAction.tenant_id == tid, RFQAction.status == RFQActionStatus.draft,
    ).order_by(desc(RFQAction.created_at)))
    for a in r.scalars().all():
        cand = (await db.execute(select(SupplierCandidate).where(SupplierCandidate.id == a.supplier_candidate_id))).scalar_one_or_none()
        pending.append(_item(
            "rfq", RFQ_LABELS.get(a.kind, a.kind), cand.company_name if cand else "Kandidat",
            f"An {a.recipient_email}: {(a.rendered_subject or '')[:120]}",
            f"/rfq-dashboard#rfq/{a.rfq_id}", a.created_at,
        ))

    # Teil B9: Vertrags-Mails (Entwurfsversand, Klausel-Gegenvorschlag)
    r = await db.execute(select(ContractAction).where(
        ContractAction.tenant_id == tid, ContractAction.status == ContractActionStatus.draft,
    ).order_by(desc(ContractAction.created_at)))
    for a in r.scalars().all():
        pending.append(_item(
            "contract", CONTRACT_LABELS.get(a.kind, a.kind), "Vertrag",
            f"An {a.recipient_email}: {(a.rendered_subject or '')[:120]}",
            f"/rfq-dashboard#contract/{a.contract_id}", a.created_at,
        ))

    # Master Data Center: vorgeschlagene Preispositionen, je Dokumentversion gebuendelt
    r = await db.execute(select(MDCLineItem, MDCDocumentVersion, MDCDocument).join(
        MDCDocumentVersion, MDCLineItem.document_version_id == MDCDocumentVersion.id,
    ).join(MDCDocument, MDCDocumentVersion.document_id == MDCDocument.id).where(
        MDCLineItem.tenant_id == tid,
        MDCLineItem.review_status.in_([MDCReviewStatus.extracted, MDCReviewStatus.needs_review]),
    ))
    by_version: dict = {}
    for item, version, doc in r.all():
        entry = by_version.setdefault(version.id, {"doc": doc, "version": version, "open": 0, "blocked": 0, "latest": item.created_at})
        entry["open"] += 1
        if item.review_status == MDCReviewStatus.needs_review:
            entry["blocked"] += 1
        if item.created_at and (entry["latest"] is None or item.created_at > entry["latest"]):
            entry["latest"] = item.created_at
    for entry in by_version.values():
        pending.append(_item(
            "mdc", "Preispositionen pruefen", f"{entry['doc'].title or 'Dokument'} (v{entry['version'].version_number})",
            f"{entry['open']} offen, davon {entry['blocked']} mit blockierenden Punkten",
            f"/data-center#doc/{entry['doc'].id}", entry["latest"],
        ))

    pending.sort(key=lambda x: x["created_at"] or "", reverse=True)

    # Juengste Aktivitaet -- was der Agent zuletzt tatsaechlich getan hat (Status-Uebergaenge), ueber Module hinweg
    activity = []
    r = await db.execute(select(CaseEvent).where(CaseEvent.tenant_id == tid).order_by(desc(CaseEvent.created_at)).limit(15))
    for e in r.scalars().all():
        activity.append({"when": e.created_at.isoformat() if e.created_at else None,
                          "text": f"Fall-Status: {e.to_status}" + (f" ({e.reason})" if e.reason else ""), "actor": e.actor})
    r = await db.execute(select(NDAEvent).where(NDAEvent.tenant_id == tid).order_by(desc(NDAEvent.created_at)).limit(15))
    for e in r.scalars().all():
        activity.append({"when": e.created_at.isoformat() if e.created_at else None,
                          "text": f"NDA-Status: {e.to_status}" + (f" ({e.reason})" if e.reason else ""), "actor": e.actor})
    activity.sort(key=lambda x: x["when"] or "", reverse=True)

    return {
        "pending_count": len(pending),
        "pending": pending[:100],
        "recent_activity": activity[:25],
    }
