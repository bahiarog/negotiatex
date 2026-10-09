"""
Vorhaben (Customer Journey) -- die Kundensicht ueber alle Module.

Zwei Einstiege:
  A) Neuer Bedarf: Briefing per Freitext oder Dokument -> Entwurf mit offenen
     Fragen -> Kunde bestaetigt -> Agent schlaegt interne Dienstleister vor ->
     Kunde waehlt aus -> Erstkontakt-Entwuerfe -> (Freigabe) -> Antworten,
     Stammdaten, NDA, Angebotsanfrage -> Angebote -> Pruefung, Verhandlung ->
     Kunde entscheidet.
  B) Bestehendes Angebot: Angebot hochladen -> Pruefung (AGB/Compliance),
     Verhandlung mit dem Anbieter und auf Wunsch Vergleichsangebote.

Dieser Router fuehrt keine eigene Versandlogik ein. Jede ausgehende Mail
bleibt ein Entwurf im jeweiligen Modul und wird ueber dessen hash-gebundene
Freigabe versendet; die Detailansicht liefert dafuer je Entwurf den
Freigabe-Endpunkt. Die Entscheidung (Zuschlag) trifft ausschliesslich der
Kunde ueber POST /projects/{id}/award.
"""
import asyncio
import logging
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from deps import get_current_membership, get_current_user
from models_contracts import (
    RFQ, RFQAction, RFQActionStatus, RFQInvitation, RFQOffer, RFQStatus, OfferStatus,
)
from models_mdc import MDCDocument, MDCSupplier
from models_negotiation import NegotiationAction, NegotiationActionStatus, NegotiationStrategy, NegotiationException
from models_projects import OfferReview, Project, ProjectEvent, ProjectStatus
from models_sourcing import (
    NDA, NDAStatus, CandidateStatus, OutreachAction, OutreachActionStatus, OutreachMessage, OutreachDirection,
    SourcingRequest, SourcingRequestStatus, SupplierCandidate,
)
from models_v2 import Case, CaseEvent, CaseStatus, SupplierV2
from services.projects import add_event

logger = logging.getLogger(__name__)
router = APIRouter()

STATUS_LABEL = {
    "briefing": "Briefing", "sourcing": "Dienstleistersuche", "collecting_offers": "Angebote einholen",
    "evaluating": "Pruefen & Verhandeln", "decision": "Entscheidung", "awarded": "Beauftragt", "cancelled": "Abgebrochen",
}
CANDIDATE_LABEL = {
    "found": "vorgeschlagen", "shortlisted": "ausgewaehlt", "contacted": "angeschrieben", "responded": "hat geantwortet",
    "interested": "interessiert", "declined": "abgesagt", "onboarding_pending": "Stammdaten/Anfrage laufen",
    "nda_review": "NDA in Arbeit", "nda_approved": "NDA freigegeben", "qualified": "qualifiziert", "rejected": "ausgeschieden",
}

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


async def _get_project(project_id: str, tenant_id, db: AsyncSession) -> Project:
    try:
        pid = uuid.UUID(str(project_id))
    except ValueError:
        raise HTTPException(404, "Vorhaben nicht gefunden.")
    p = (await db.execute(select(Project).where(Project.id == pid))).scalar_one_or_none()
    if not p or p.tenant_id != tenant_id:
        raise HTTPException(404, "Vorhaben nicht gefunden.")
    return p


def _project_dict(p: Project) -> dict:
    return {
        "id": str(p.id), "title": p.title, "entry_path": p.entry_path, "status": _v(p.status),
        "status_label": STATUS_LABEL.get(_v(p.status), _v(p.status)),
        "service_type": p.service_type, "description": p.description, "must_criteria": p.must_criteria_json or {},
        "conditions_text": p.conditions_text, "budget_target": _s(p.budget_target), "budget_ceiling": _s(p.budget_ceiling),
        "currency": p.currency, "needed_by": p.needed_by.isoformat() if p.needed_by else None, "region": p.region,
        "delivery_location": p.delivery_location, "quantity": _s(p.quantity), "unit": p.unit,
        "briefing_source": p.briefing_source, "briefing_document_id": _s(p.briefing_document_id),
        "open_questions": p.open_questions_json or [], "sourcing_request_id": _s(p.sourcing_request_id),
        "rfq_id": _s(p.rfq_id), "seek_alternatives": p.seek_alternatives, "customer_email": p.customer_email,
        "notify_milestones": p.notify_milestones, "awarded_offer_id": _s(p.awarded_offer_id),
        "awarded_at": p.awarded_at.isoformat() if p.awarded_at else None, "decision_note": p.decision_note,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


async def _fresh(db: AsyncSession, p: Project) -> dict:
    """Nach commit: serverseitig gesetzte Felder (updated_at) neu laden."""
    await db.refresh(p)
    return _project_dict(p)


def _dec(v) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v).replace(",", "."))
    except Exception:
        raise HTTPException(400, f"Ungueltiger Betrag: {v}")


async def _read_upload(file: UploadFile, max_mb: int = 15) -> bytes:
    data = await file.read()
    if not data:
        raise HTTPException(400, "Datei ist leer.")
    if len(data) > max_mb * 1024 * 1024:
        raise HTTPException(400, f"Datei zu gross (max. {max_mb} MB).")
    return data


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


# ---------------------------------------------------------------------------
# Briefing
# ---------------------------------------------------------------------------

@router.post("/briefing/draft")
async def briefing_draft(text: Optional[str] = Form(None), file: Optional[UploadFile] = File(None),
                         user=Depends(get_current_user), membership=Depends(get_current_membership),
                         db: AsyncSession = Depends(get_db)):
    """Freitext und/oder Briefing-Dokument -> strukturierter Entwurf. Ein
    Dokument wird im Data Center abgelegt (nachweisbares Original)."""
    from services.briefing import draft_briefing
    source, doc_id, doc_text = "prompt", None, ""
    if file is not None and file.filename:
        from services.offer_intake import store_offer_file
        from models_mdc import MDCDocumentType
        data = await _read_upload(file)
        try:
            doc, _ver, doc_text = await store_offer_file(db, membership.tenant_id, file.filename, data, None,
                                                         actor=str(user.id), source="briefing")
        except ValueError as e:
            raise HTTPException(400, str(e))
        doc.document_type = MDCDocumentType.other
        doc.usage_purpose = "Briefing eines Vorhabens"
        doc_id = str(doc.id)
        source = "document"
        await db.commit()
        if not doc_text:
            raise HTTPException(400, "Dokument konnte nicht gelesen werden -- bitte Bedarf als Text beschreiben.")
    combined = "\n\n".join(x for x in [(text or "").strip(), doc_text] if x)
    if len(combined) < 15:
        raise HTTPException(400, "Bitte den Bedarf etwas ausfuehrlicher beschreiben oder ein Dokument hochladen.")
    draft = await asyncio.to_thread(draft_briefing, combined, source)
    draft.update({"briefing_source": source, "briefing_document_id": doc_id})
    return draft


class ProjectCreate(BaseModel):
    title: str
    service_type: Optional[str] = None
    description: str
    must_criteria: dict = {}
    conditions_text: Optional[str] = None
    budget_target: Optional[str] = None
    budget_ceiling: Optional[str] = None
    currency: str = "EUR"
    needed_by: Optional[date] = None
    region: Optional[str] = None
    delivery_location: Optional[str] = None
    quantity: Optional[str] = None
    unit: Optional[str] = None
    briefing_source: Optional[str] = "prompt"
    briefing_input: Optional[str] = None
    briefing_document_id: Optional[str] = None
    open_questions: list = []
    notify_milestones: bool = True


def _apply_fields(p: Project, d: ProjectCreate):
    if not d.title.strip() or not d.description.strip():
        raise HTTPException(400, "Titel und Beschreibung sind Pflicht.")
    p.title, p.service_type, p.description = d.title.strip()[:255], d.service_type, d.description.strip()
    p.must_criteria_json = {str(k): str(v) for k, v in (d.must_criteria or {}).items() if str(k).strip()}
    p.conditions_text = d.conditions_text
    p.budget_target, p.budget_ceiling = _dec(d.budget_target), _dec(d.budget_ceiling)
    if p.budget_target and p.budget_ceiling and p.budget_target > p.budget_ceiling:
        raise HTTPException(400, "Zielbudget darf nicht ueber der Budgetobergrenze liegen.")
    p.currency = (d.currency or "EUR")[:10]
    p.needed_by, p.region, p.delivery_location = d.needed_by, d.region, d.delivery_location
    p.quantity, p.unit = _dec(d.quantity), d.unit
    p.open_questions_json = [str(q) for q in (d.open_questions or [])][:10]
    p.notify_milestones = d.notify_milestones


@router.post("")
async def create_project(payload: ProjectCreate, user=Depends(get_current_user),
                         membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    p = Project(tenant_id=membership.tenant_id, title=payload.title[:255] or "Vorhaben", entry_path="new_need",
                status=ProjectStatus.briefing, customer_email=getattr(user, "email", None), created_by=str(user.id),
                briefing_source=payload.briefing_source, briefing_input=(payload.briefing_input or "")[:20000] or None)
    _apply_fields(p, payload)
    if payload.briefing_document_id:
        try:
            p.briefing_document_id = uuid.UUID(payload.briefing_document_id)
        except ValueError:
            pass
    db.add(p)
    await db.flush()
    await add_event(db, p, "created", "Vorhaben angelegt", "Briefing-Entwurf erstellt -- bitte pruefen und Agent starten.",
                    actor=str(user.id))
    await db.commit()
    return await _fresh(db, p)


@router.put("/{project_id}")
async def update_project(project_id: str, payload: ProjectCreate, user=Depends(get_current_user),
                         membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    p = await _get_project(project_id, membership.tenant_id, db)
    if _v(p.status) in ("awarded", "cancelled"):
        raise HTTPException(400, "Abgeschlossene Vorhaben koennen nicht mehr geaendert werden.")
    _apply_fields(p, payload)
    if p.sourcing_request_id:
        await _ensure_request(db, p, str(user.id))
    await add_event(db, p, "briefing_updated", "Briefing aktualisiert", actor=str(user.id))
    await db.commit()
    return await _fresh(db, p)


@router.get("")
async def list_projects(membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Project).where(Project.tenant_id == membership.tenant_id)
                             .order_by(desc(Project.updated_at)))).scalars().all()
    out = []
    for p in rows:
        d = _project_dict(p)
        d["todo_count"] = len((await _pending_drafts(db, p)))
        last = (await db.execute(select(ProjectEvent).where(ProjectEvent.project_id == p.id)
                                 .order_by(desc(ProjectEvent.created_at)).limit(1))).scalars().first()
        d["last_event"] = {"title": last.title, "at": last.created_at.isoformat() if last.created_at else None} if last else None
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Start + interne Dienstleister-Vorschlaege
# ---------------------------------------------------------------------------

@router.post("/{project_id}/start")
async def start_project(project_id: str, user=Depends(get_current_user),
                        membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """Kunde bestaetigt das Briefing und beauftragt den Agenten. Das ist die
    Kampagnenfreigabe fuer den Suchauftrag (Rahmen: Briefing, Limits)."""
    p = await _get_project(project_id, membership.tenant_id, db)
    if _v(p.status) != "briefing":
        raise HTTPException(400, "Der Agent wurde fuer dieses Vorhaben bereits gestartet.")
    if not p.budget_ceiling:
        raise HTTPException(400, "Bitte eine Budgetobergrenze angeben -- sie ist der Verhandlungsrahmen des Agenten.")
    req = await _ensure_request(db, p, str(user.id))
    req.status = SourcingRequestStatus.approved
    req.approved_by, req.approved_at = str(user.id), datetime.utcnow()
    p.status = ProjectStatus.sourcing
    await add_event(db, p, "started", "Agent beauftragt",
                    "Der Agent sucht passende Dienstleister im eigenen Bestand und bereitet Anfragen vor. "
                    "Jede E-Mail wird Ihnen vor dem Versand zur Freigabe vorgelegt.", milestone=True, actor=str(user.id))
    await db.commit()
    return await _fresh(db, p)


@router.get("/{project_id}/suggestions")
async def suggestions(project_id: str, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """Interne Recherche: Lieferantenstamm, frueher angefragte Dienstleister,
    Dienstleister aus dem Data Center. Rangfolge per lokaler semantischer
    Aehnlichkeit zum Briefing -- ein Vorschlag, keine Auswahl."""
    p = await _get_project(project_id, membership.tenant_id, db)
    tid = membership.tenant_id
    existing_emails, existing_names = set(), set()
    if p.sourcing_request_id:
        for c in (await db.execute(select(SupplierCandidate).where(SupplierCandidate.sourcing_request_id == p.sourcing_request_id))).scalars().all():
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
        status = _v(c.status)
        put(key, {"source": "candidate", "ref_id": str(c.id), "company_name": c.company_name, "contact_email": c.contact_email,
                  "contact_name": c.contact_name, "reasons": [f"Frueher angefragt: \"{r.title[:60]}\" ({CANDIDATE_LABEL.get(status, status)})"],
                  "profile": " ".join(x for x in [c.company_name, c.service_match_note or "", r.title, r.bedarf_text[:300]] if x)})
    docs = (await db.execute(select(MDCSupplier, MDCDocument).join(MDCDocument, MDCDocument.supplier_id == MDCSupplier.id)
                             .where(MDCSupplier.tenant_id == tid))).all()
    mdc_titles: dict = {}
    for s, d in docs:
        mdc_titles.setdefault(s.id, (s, []))[1].append(d.title or "")
    for sid, (s, titles) in mdc_titles.items():
        match = next((k for k, v in pool.items() if v["company_name"].strip().lower() == s.name.strip().lower()), None)
        entry = {"source": "mdc", "ref_id": str(sid), "company_name": s.name, "contact_email": None, "contact_name": None,
                 "reasons": [f"Im Data Center: {len(titles)} Dokument(e)"], "profile": s.name + " " + " ".join(titles[:5])}
        put(match or f"mdc:{sid}", entry)

    items = [e for e in pool.values() if (e["contact_email"] or "").lower() not in existing_emails
             and e["company_name"].strip().lower() not in existing_names]
    if not items:
        return {"items": [], "note": "Keine weiteren internen Dienstleister gefunden. Sie koennen Dienstleister manuell hinzufuegen."}
    query = " ".join(x for x in [p.title, p.service_type or "", p.description or "", " ".join(p.must_criteria_json.values()) if p.must_criteria_json else ""] if x)
    try:
        from services.mdc_embeddings import embed_texts
        vecs = await asyncio.to_thread(embed_texts, [query] + [e["profile"][:2000] for e in items])
        q = vecs[0]
        for e, v in zip(items, vecs[1:]):
            e["score"] = round(sum(a * b for a, b in zip(q, v)), 3)
    except Exception:
        logger.exception("Vorschlags-Ranking ohne Embeddings")
        for e in items:
            e["score"] = None
    items.sort(key=lambda e: (e["score"] is None, -(e["score"] or 0)))
    for e in items:
        e.pop("profile", None)
        e["contactable"] = bool(e["contact_email"])
    return {"items": items[:20], "note": "Rangfolge nach inhaltlicher Naehe zum Briefing. Externe Recherche folgt in einer spaeteren Ausbaustufe."}


class CandidateSelection(BaseModel):
    source: str                      # supplier | candidate | mdc | manual
    ref_id: Optional[str] = None
    company_name: str
    contact_email: Optional[str] = None
    contact_name: Optional[str] = None
    note: Optional[str] = None
    website: Optional[str] = None


class SelectPayload(BaseModel):
    items: list[CandidateSelection]


@router.post("/{project_id}/candidates")
async def select_candidates(project_id: str, payload: SelectPayload, user=Depends(get_current_user),
                            membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """Kunde waehlt Dienstleister aus (interne Vorschlaege oder manuell).
    Fuer jeden entsteht ein Erstkontakt-ENTWURF -- versendet wird erst nach Freigabe."""
    p = await _get_project(project_id, membership.tenant_id, db)
    if _v(p.status) in ("briefing", "awarded", "cancelled"):
        raise HTTPException(400, "Erst den Agenten starten (Briefing bestaetigen).")
    req = await _ensure_request(db, p, str(user.id))
    existing = (await db.execute(select(SupplierCandidate).where(SupplierCandidate.sourcing_request_id == req.id))).scalars().all()
    if len([c for c in existing if _v(c.status) != "rejected"]) + len(payload.items) > req.max_candidates_total:
        raise HTTPException(409, f"Hoechstens {req.max_candidates_total} Dienstleister je Vorhaben.")
    seen = {(c.contact_email or "").lower() for c in existing if c.contact_email}
    created, skipped = [], []
    for it in payload.items:
        email = (it.contact_email or "").strip().lower() or None
        if email and ("@" not in email or "." not in email.split("@")[-1]):
            skipped.append({"company_name": it.company_name, "reason": "E-Mail-Adresse ungueltig"})
            continue
        if email and email in seen:
            skipped.append({"company_name": it.company_name, "reason": "bereits im Vorhaben"})
            continue
        existing_supplier_id = None
        if it.source == "supplier" and it.ref_id:
            try:
                existing_supplier_id = uuid.UUID(it.ref_id)
            except ValueError:
                pass
        src = {"supplier": "intern:lieferantenstamm", "candidate": "intern:fruehere-anfrage", "mdc": "intern:data-center",
               "manual": it.website or "manuell:kunde"}.get(it.source, "manuell:kunde")
        crit = {k: "unknown" for k in (p.must_criteria_json or {})}
        cand = SupplierCandidate(
            tenant_id=p.tenant_id, sourcing_request_id=req.id, company_name=it.company_name.strip()[:255],
            domain=(email.split("@")[-1] if email else None), service_match_note=(it.note or p.service_type or p.title)[:500],
            contact_email=email, contact_name=it.contact_name, source_url=f"{src}/{it.ref_id}" if it.ref_id else src,
            retrieved_at=datetime.utcnow(), existing_supplier_id=existing_supplier_id,
            must_criteria_check_json=crit, open_questions_json=[f"Bitte bestaetigen Sie: {k}" for k in crit],
            status=CandidateStatus.shortlisted, created_by=str(user.id),
        )
        db.add(cand)
        await db.flush()
        if email:
            seen.add(email)
            subject, body = _first_contact(p, cand)
            db.add(OutreachAction(tenant_id=p.tenant_id, supplier_candidate_id=cand.id, kind="first_contact",
                                  reminder_number=0, recipient_email=email, rendered_subject=subject, rendered_body=body,
                                  status=OutreachActionStatus.draft, created_by=str(user.id)))
        created.append({"id": str(cand.id), "company_name": cand.company_name, "draft": bool(email)})
    if created:
        names = ", ".join(c["company_name"] for c in created)
        await add_event(db, p, "candidates_selected", f"{len(created)} Dienstleister ausgewaehlt",
                        f"{names}. Erstkontakt-Entwuerfe liegen zur Freigabe bereit.", actor=str(user.id))
    await db.commit()
    return {"created": created, "skipped": skipped}


# ---------------------------------------------------------------------------
# Detailansicht
# ---------------------------------------------------------------------------

async def _project_candidates(db: AsyncSession, p: Project) -> list[SupplierCandidate]:
    if not p.sourcing_request_id:
        return []
    return list((await db.execute(select(SupplierCandidate).where(SupplierCandidate.sourcing_request_id == p.sourcing_request_id)
                                  .order_by(SupplierCandidate.created_at))).scalars().all())


async def _project_rfqs(db: AsyncSession, p: Project) -> list[RFQ]:
    if not p.sourcing_request_id:
        return []
    return list((await db.execute(select(RFQ).where(RFQ.sourcing_request_id == p.sourcing_request_id))).scalars().all())


def _draft(kind, label, cand_name, a, approve, reject, extra=None):
    d = {"kind": kind, "label": label, "id": str(a.id), "supplier": cand_name,
         "recipient": getattr(a, "recipient_email", None), "subject": getattr(a, "rendered_subject", None),
         "body": getattr(a, "rendered_body", None), "approve": approve, "reject": reject,
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
            label = {"first_contact": "Erstkontakt", "reminder": "Erinnerung", "stammdaten_invite": "Stammdaten-Link"}.get(a.kind, a.kind)
            base = f"/api/v1/sourcing/candidates/{a.supplier_candidate_id}/outreach/{a.id}"
            out.append(_draft("outreach", label, by_id[a.supplier_candidate_id].company_name, a, base + "/approve", base + "/reject"))
        for n in (await db.execute(select(NDA).where(NDA.supplier_candidate_id.in_(list(by_id)),
                                                     NDA.status.in_([NDAStatus.draft, NDAStatus.returned, NDAStatus.review_required, NDAStatus.verified])))).scalars().all():
            st = _v(n.status)
            label = {"draft": "NDA versenden", "returned": "NDA-Ruecklauf pruefen", "review_required": "NDA: Abweichung pruefen",
                     "verified": "NDA final freigeben"}[st]
            d = {"kind": "nda", "label": label, "id": str(n.id), "supplier": by_id[n.supplier_candidate_id].company_name,
                 "recipient": by_id[n.supplier_candidate_id].contact_email, "subject": f"NDA {n.template_version}",
                 "body": n.draft_text if st == "draft" else (n.returned_text or ""), "nda_status": st,
                 "approve": f"/api/v1/sourcing/nda/{n.id}/approve-send" if st == "draft" else
                            (f"/api/v1/sourcing/nda/{n.id}/approve" if st == "verified" else f"/api/v1/sourcing/nda/{n.id}/verify"),
                 "reject": f"/api/v1/sourcing/nda/{n.id}/reject",
                 "created_at": n.updated_at.isoformat() if n.updated_at else None}
            out.append(d)
    for rfq in await _project_rfqs(db, p):
        for a in (await db.execute(select(RFQAction).where(RFQAction.rfq_id == rfq.id, RFQAction.status == RFQActionStatus.draft)
                                   .order_by(RFQAction.created_at))).scalars().all():
            label = {"invite": "Angebotsanfrage", "receipt_confirmation": "Eingangsbestaetigung", "clarification_broadcast": "Klarstellung",
                     "award_notice": "Zusage an Dienstleister", "decline_notice": "Absage an Dienstleister"}.get(a.kind, a.kind)
            c = by_id.get(a.supplier_candidate_id)
            base = f"/api/v1/rfq/{rfq.id}/actions/{a.id}"
            out.append(_draft("rfq", label, c.company_name if c else "", a, base + "/approve", base + "/reject"))
    for offer_id, case_id in (p.negotiations_json or {}).items():
        try:
            cid = uuid.UUID(case_id)
        except ValueError:
            continue
        for a in (await db.execute(select(NegotiationAction).where(NegotiationAction.case_id == cid,
                                                                   NegotiationAction.status == NegotiationActionStatus.draft))).scalars().all():
            base = f"/api/v1/negotiation/cases/{cid}/actions/{a.id}"
            offer = (await db.execute(select(RFQOffer).where(RFQOffer.id == uuid.UUID(offer_id)))).scalar_one_or_none()
            c = by_id.get(offer.supplier_candidate_id) if offer else None
            label = "Verhandlung: Preisvorschlag" if _v(a.action_type) == "propose_price" else "Verhandlung: Erinnerung"
            out.append(_draft("negotiation", label, c.company_name if c else "", a, base + "/approve", base + "/reject",
                              {"price": _s(a.proposed_price_net), "warnings": a.open_questions or []}))
    return out


def _offer_total(o: RFQOffer, p: Project) -> Optional[Decimal]:
    if o.unit_price is None:
        return None
    qty = o.quantity if o.quantity is not None else (p.quantity if p.quantity is not None else Decimal("1"))
    return (Decimal(o.unit_price) * Decimal(qty) + Decimal(o.freight_cost or 0) + Decimal(o.other_costs or 0)).quantize(Decimal("0.01"))


async def _offers(db: AsyncSession, p: Project, cands: dict) -> list[dict]:
    out = []
    for rfq in await _project_rfqs(db, p):
        offers = (await db.execute(select(RFQOffer).where(RFQOffer.rfq_id == rfq.id).order_by(RFQOffer.received_at))).scalars().all()
        for o in offers:
            if _v(o.status) != "submitted":
                continue
            history = [x for x in offers if x.supplier_candidate_id == o.supplier_candidate_id and x.id != o.id]
            first = min(history + [o], key=lambda x: x.version)
            rv = (await db.execute(select(OfferReview).where(OfferReview.offer_id == o.id).order_by(desc(OfferReview.created_at)))).scalars().first()
            if rv is None and history:  # neue Version nach Verhandlung: letzte Pruefung der Vorversion zeigen
                rv = (await db.execute(select(OfferReview).where(OfferReview.offer_id.in_([h.id for h in history]))
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
            total, first_total = _offer_total(o, p), _offer_total(first, p)
            out.append({
                "id": str(o.id), "rfq_id": str(rfq.id), "version": o.version, "supplier": c.company_name if c else "",
                "candidate_id": str(o.supplier_candidate_id), "unit_price": _s(o.unit_price), "quantity": _s(o.quantity),
                "freight_cost": _s(o.freight_cost), "other_costs": _s(o.other_costs), "currency": o.currency,
                "total": _s(total), "first_total": _s(first_total) if o.version > 1 else None,
                "saving": _s(first_total - total) if (o.version > 1 and total is not None and first_total is not None) else None,
                "delivery_date": o.delivery_date, "payment_terms": o.payment_terms,
                "offer_validity_until": o.offer_validity_until.date().isoformat() if o.offer_validity_until else None,
                "scope_note": o.scope_note, "spec_confirmed": o.spec_confirmed,
                "comparability": _v(o.comparability_flag), "comparability_note": o.comparability_note,
                "within_budget": (total <= p.budget_ceiling) if (total is not None and p.budget_ceiling) else None,
                "review": {"status": rv.status, "findings": rv.findings_json or [], "at": rv.created_at.isoformat() if rv.created_at else None} if rv else None,
                "document_id": str(doc.id) if doc else None, "negotiation": neg,
                "received_at": o.received_at.isoformat() if o.received_at else None,
                "awarded": p.awarded_offer_id == o.id,
            })
    out.sort(key=lambda x: (x["total"] is None, Decimal(x["total"]) if x["total"] else 0))
    return out


def _stage(p: Project, offers: list, cands: list) -> int:
    st = _v(p.status)
    if st == "awarded":
        return 5
    if st == "decision":
        return 4
    if offers:
        return 3
    if cands:
        return 2
    return 1 if st != "briefing" else 0


def _todos(p: Project, drafts: list, offers: list, cands: list) -> list[dict]:
    t = []
    st = _v(p.status)
    if st == "briefing":
        t.append({"kind": "start", "text": "Briefing pruefen, offene Fragen beantworten und Agent starten."})
    if drafts:
        t.append({"kind": "approve", "text": f"{len(drafts)} Entwurf/Entwuerfe warten auf Ihre Freigabe."})
    if st in ("sourcing", "collecting_offers", "evaluating") and not [c for c in cands if _v(c.status) not in ("rejected", "declined")]:
        t.append({"kind": "select", "text": "Dienstleister auswaehlen (Vorschlaege des Agenten oder manuell)."})
    crit = [o for o in offers if o["review"] and o["review"]["status"] == "critical"]
    if crit:
        t.append({"kind": "review", "text": f"{len(crit)} Angebot(e) mit kritischen Pruefpunkten ansehen."})
    if offers and st not in ("awarded", "cancelled"):
        t.append({"kind": "decide", "text": "Angebote vergleichen und entscheiden, wer beauftragt wird (oder zuerst verhandeln lassen)."})
    return t


@router.get("/{project_id}")
async def get_project(project_id: str, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    p = await _get_project(project_id, membership.tenant_id, db)
    cands = await _project_candidates(db, p)
    by_id = {c.id: c for c in cands}
    drafts = await _pending_drafts(db, p)
    offers = await _offers(db, p, by_id)
    cand_rows = []
    for c in cands:
        msgs = (await db.execute(select(OutreachMessage).where(OutreachMessage.supplier_candidate_id == c.id)
                                 .order_by(desc(OutreachMessage.occurred_at)))).scalars().all()
        nda = (await db.execute(select(NDA).where(NDA.supplier_candidate_id == c.id))).scalar_one_or_none()
        inv = (await db.execute(select(RFQInvitation).where(RFQInvitation.supplier_candidate_id == c.id)
                                .order_by(desc(RFQInvitation.created_at)))).scalars().first()
        st = _v(c.status)
        cand_rows.append({
            "id": str(c.id), "company_name": c.company_name, "contact_email": c.contact_email, "contact_name": c.contact_name,
            "status": st, "status_label": CANDIDATE_LABEL.get(st, st), "source": (c.source_url or "").split("/")[0],
            "is_incumbent": p.incumbent_candidate_id == c.id,
            "messages": [{"direction": _v(m.direction), "subject": m.subject, "body": (m.body_text or "")[:1500],
                          "at": m.occurred_at.isoformat() if m.occurred_at else None} for m in msgs[:10]],
            "nda_status": _v(nda.status) if nda else None,
            "rfq_invited": bool(inv and inv.sent_at), "open_questions": (c.open_questions_json or [])[-3:],
            "has_offer": any(o["candidate_id"] == str(c.id) for o in offers),
        })
    events = (await db.execute(select(ProjectEvent).where(ProjectEvent.project_id == p.id)
                               .order_by(desc(ProjectEvent.created_at)))).scalars().all()
    return {
        "project": _project_dict(p), "stage": _stage(p, offers, cands), "todos": _todos(p, drafts, offers, cands),
        "drafts": drafts, "candidates": cand_rows, "offers": offers,
        "events": [{"kind": e.kind, "title": e.title, "detail": e.detail, "actor": e.actor, "milestone": e.milestone,
                    "emailed": bool(e.emailed_at), "at": e.created_at.isoformat() if e.created_at else None} for e in events],
    }


# ---------------------------------------------------------------------------
# Angebote: Upload, Pruefung, Verhandlung
# ---------------------------------------------------------------------------

@router.post("/{project_id}/offers/upload")
async def upload_offer(project_id: str, candidate_id: str = Form(...), file: UploadFile = File(...),
                       user=Depends(get_current_user), membership=Depends(get_current_membership),
                       db: AsyncSession = Depends(get_db)):
    """Angebot, das auf anderem Weg kam (z.B. Post, Portal), manuell zuordnen."""
    from services.offer_intake import store_offer_file, intake_offer
    p = await _get_project(project_id, membership.tenant_id, db)
    try:
        cid = uuid.UUID(candidate_id)
    except ValueError:
        raise HTTPException(404, "Dienstleister nicht gefunden.")
    cand = (await db.execute(select(SupplierCandidate).where(SupplierCandidate.id == cid))).scalar_one_or_none()
    if not cand or cand.tenant_id != p.tenant_id or cand.sourcing_request_id != p.sourcing_request_id:
        raise HTTPException(404, "Dienstleister gehoert nicht zu diesem Vorhaben.")
    data = await _read_upload(file)
    req = await _ensure_request(db, p, str(user.id))
    rfq = await _ensure_rfq(db, p, req, str(user.id))
    try:
        doc, _v2, text = await store_offer_file(db, p.tenant_id, file.filename or "angebot.pdf", data, cand.company_name,
                                                category_id=p.category_id, actor=str(user.id), source="upload")
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not text:
        raise HTTPException(400, "Datei konnte nicht gelesen werden.")
    offer, review = await intake_offer(db, rfq, cand, text, actor=str(user.id), doc=doc, channel="Upload", draft_receipt=False)
    await db.commit()
    return {"offer_id": str(offer.id), "review": review.status}


@router.post("/{project_id}/offers/{offer_id}/review")
async def rerun_review(project_id: str, offer_id: str, user=Depends(get_current_user),
                       membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    from services.offer_intake import run_review
    from models_mdc import MDCDocumentVersion
    p = await _get_project(project_id, membership.tenant_id, db)
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
    rv = await run_review(db, offer, p, text, actor=str(user.id))
    await db.commit()
    return {"status": rv.status, "findings": rv.findings_json}


async def _get_offer(db: AsyncSession, p: Project, offer_id: str) -> RFQOffer:
    try:
        oid = uuid.UUID(offer_id)
    except ValueError:
        raise HTTPException(404, "Angebot nicht gefunden.")
    o = (await db.execute(select(RFQOffer).where(RFQOffer.id == oid))).scalar_one_or_none()
    rfq_ids = [r.id for r in await _project_rfqs(db, p)]
    if not o or o.tenant_id != p.tenant_id or o.rfq_id not in rfq_ids:
        raise HTTPException(404, "Angebot gehoert nicht zu diesem Vorhaben.")
    return o


class NegotiatePayload(BaseModel):
    round1_price: Optional[str] = None
    round2_price: Optional[str] = None
    usage_rights_text: Optional[str] = None


@router.post("/{project_id}/offers/{offer_id}/negotiate")
async def negotiate_offer(project_id: str, offer_id: str, payload: NegotiatePayload, user=Depends(get_current_user),
                          membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """Verhandlung VOR der Entscheidung, ueber die bestehende Teil-A-Engine.
    Der Kunde gibt die Rundenpreise frei (Vorschlag aus Budget); Obergrenze
    ist der Angebotspreis -- es wird nie mehr geboten als angeboten."""
    from routers.cases import _transition as case_transition
    from routers.negotiation import draft_action
    p = await _get_project(project_id, membership.tenant_id, db)
    offer = await _get_offer(db, p, offer_id)
    if _v(offer.status) != "submitted":
        raise HTTPException(400, "Nur das aktuelle Angebot kann verhandelt werden.")
    if str(offer.id) in (p.negotiations_json or {}):
        raise HTTPException(400, "Fuer dieses Angebot laeuft bereits eine Verhandlung.")
    total = _offer_total(offer, p)
    if total is None:
        raise HTTPException(400, "Angebot ohne erkannten Preis -- bitte zuerst Preis ergaenzen.")
    cand = (await db.execute(select(SupplierCandidate).where(SupplierCandidate.id == offer.supplier_candidate_id))).scalar_one()
    if not cand.contact_email:
        raise HTTPException(400, "Keine Kontaktadresse des Dienstleisters hinterlegt.")
    q2 = Decimal("0.01")
    if payload.round1_price:
        r1 = _dec(payload.round1_price)
    elif p.budget_target and p.budget_target < total:
        r1 = max(Decimal(p.budget_target), total * Decimal("0.85"))
    else:
        r1 = total * Decimal("0.90")
    r1 = r1.quantize(q2)
    r2 = (_dec(payload.round2_price) if payload.round2_price else (r1 + total) / 2).quantize(q2)
    if not (Decimal("0") < r1 <= r2 <= total):
        raise HTTPException(400, f"Rundenpreise muessen 0 < Runde 1 <= Runde 2 <= Angebotspreis ({total}) erfuellen.")

    case = Case(tenant_id=p.tenant_id, title=f"{p.title} — Verhandlung {cand.company_name}"[:255],
                category="project_negotiation", status=CaseStatus.RECEIVED, case_version=1)
    db.add(case)
    await db.flush()
    db.add(CaseEvent(case_id=case.id, tenant_id=case.tenant_id, from_status=None, to_status=CaseStatus.RECEIVED.value,
                     actor=str(user.id), reason=f"Verhandlung im Vorhaben {p.id} (Angebot {offer.id}, vor Entscheidung)."))
    db.add(NegotiationStrategy(
        tenant_id=p.tenant_id, case_id=case.id, starting_price_net=total, round1_price_net=r1, round2_price_net=r2,
        price_cap_net=total, currency=offer.currency or p.currency or "EUR",
        scope_text=(p.description or p.title)[:1000],
        usage_rights_text=payload.usage_rights_text or p.conditions_text or "wie im Angebot beschrieben, unveraendert",
        delivery_date=offer.delivery_date or (p.needed_by.strftime("%d.%m.%Y") if p.needed_by else "wie angeboten"),
        max_rounds=2, supplier_email=cand.contact_email.lower(), created_by=str(user.id),
    ))
    await case_transition(db, case, CaseStatus.READY_TO_DRAFT, actor=str(user.id), reason="Verhandlungsrahmen vom Kunden bestaetigt.")
    p.negotiations_json = {**(p.negotiations_json or {}), str(offer.id): str(case.id)}
    if _v(p.status) in ("sourcing", "collecting_offers"):
        p.status = ProjectStatus.evaluating
    await add_event(db, p, "negotiation_started", f"Verhandlung mit {cand.company_name} vorbereitet",
                    f"Runde 1: {r1} {offer.currency}, Runde 2: {r2} {offer.currency} (Angebot: {total}). "
                    "Der erste Preisvorschlag liegt zur Freigabe bereit.", actor=str(user.id))
    await db.commit()
    draft = await draft_action(case_id=str(case.id), user=user, membership=membership, db=db)
    return {"case_id": str(case.id), "round1": str(r1), "round2": str(r2), "draft": draft}


@router.post("/{project_id}/offers/{offer_id}/next-round")
async def next_round(project_id: str, offer_id: str, user=Depends(get_current_user),
                     membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    from routers.negotiation import draft_action
    p = await _get_project(project_id, membership.tenant_id, db)
    case_id = (p.negotiations_json or {}).get(offer_id)
    if not case_id:
        raise HTTPException(404, "Keine Verhandlung zu diesem Angebot.")
    return await draft_action(case_id=case_id, user=user, membership=membership, db=db)


class NegotiatedResult(BaseModel):
    total_price: str
    note: Optional[str] = None


@router.post("/{project_id}/offers/{offer_id}/record-result")
async def record_result(project_id: str, offer_id: str, payload: NegotiatedResult, user=Depends(get_current_user),
                        membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """Verhandlungsergebnis als neue Angebotsversion uebernehmen (die alte
    bleibt als Historie erhalten). Der Kunde bestaetigt den Betrag."""
    p = await _get_project(project_id, membership.tenant_id, db)
    offer = await _get_offer(db, p, offer_id)
    if _v(offer.status) != "submitted":
        raise HTTPException(400, "Nur das aktuelle Angebot kann aktualisiert werden.")
    total = _dec(payload.total_price)
    old_total = _offer_total(offer, p)
    if total is None or total <= 0:
        raise HTTPException(400, "Betrag fehlt.")
    qty = offer.quantity if offer.quantity is not None else (p.quantity if p.quantity is not None else Decimal("1"))
    extra = Decimal(offer.freight_cost or 0) + Decimal(offer.other_costs or 0)
    unit = ((total - extra) / Decimal(qty)).quantize(Decimal("0.0001"))
    new = RFQOffer(
        tenant_id=offer.tenant_id, rfq_id=offer.rfq_id, supplier_candidate_id=offer.supplier_candidate_id,
        version=offer.version + 1, unit_price=unit, quantity=offer.quantity, freight_cost=offer.freight_cost,
        other_costs=offer.other_costs, currency=offer.currency, delivery_date=offer.delivery_date,
        payment_terms=offer.payment_terms, offer_validity_until=offer.offer_validity_until, scope_note=offer.scope_note,
        spec_confirmed=offer.spec_confirmed, comparability_flag=offer.comparability_flag,
        comparability_note=offer.comparability_note, raw_extracted_json={"source": "verhandlungsergebnis", "note": payload.note},
        created_by=str(user.id),
    )
    db.add(new)
    await db.flush()
    offer.status = OfferStatus.superseded
    offer.superseded_by_id = new.id
    if str(offer.id) in (p.negotiations_json or {}):
        p.negotiations_json = {**p.negotiations_json, str(new.id): p.negotiations_json[str(offer.id)]}
    cand = (await db.execute(select(SupplierCandidate).where(SupplierCandidate.id == offer.supplier_candidate_id))).scalar_one()
    saving = (old_total - total) if old_total is not None else None
    await add_event(db, p, "negotiation_result", f"Verhandlungsergebnis {cand.company_name}: {total} {offer.currency}",
                    f"Ersparnis gegenueber Erstangebot: {saving} {offer.currency}." if saving is not None else None,
                    milestone=True, actor=str(user.id))
    await db.commit()
    return {"offer_id": str(new.id), "version": new.version}


# ---------------------------------------------------------------------------
# Entscheidung
# ---------------------------------------------------------------------------

class AwardPayload(BaseModel):
    offer_id: str
    note: Optional[str] = None


@router.post("/{project_id}/award")
async def award(project_id: str, payload: AwardPayload, user=Depends(get_current_user),
                membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """Die Entscheidung des Kunden. Erzeugt Zusage-/Absage-ENTWUERFE an die
    Dienstleister (Versand erst nach Freigabe) -- keine Verhandlung mehr,
    kein Vertragsschluss durch das System."""
    p = await _get_project(project_id, membership.tenant_id, db)
    if _v(p.status) in ("awarded", "cancelled"):
        raise HTTPException(400, "Vorhaben ist bereits abgeschlossen.")
    offer = await _get_offer(db, p, payload.offer_id)
    if _v(offer.status) != "submitted":
        raise HTTPException(400, "Bitte die aktuelle Angebotsversion waehlen.")
    rfq = (await db.execute(select(RFQ).where(RFQ.id == offer.rfq_id))).scalar_one()
    winner = (await db.execute(select(SupplierCandidate).where(SupplierCandidate.id == offer.supplier_candidate_id))).scalar_one()
    p.status, p.awarded_offer_id, p.awarded_at, p.awarded_by = ProjectStatus.awarded, offer.id, datetime.utcnow(), str(user.id)
    p.decision_note = payload.note
    rfq.status, rfq.awarded_offer_id, rfq.awarded_by, rfq.awarded_at = RFQStatus.awarded, offer.id, str(user.id), datetime.utcnow()
    total = _offer_total(offer, p)
    if winner.contact_email:
        db.add(RFQAction(
            tenant_id=p.tenant_id, rfq_id=rfq.id, supplier_candidate_id=winner.id, kind="award_notice",
            recipient_email=winner.contact_email, rendered_subject=f"Zusage: {p.title} / {str(winner.id)[:8]}",
            rendered_body=(f"Guten Tag,\n\nvielen Dank fuer Ihr Angebot. Wir freuen uns, Ihnen mitzuteilen, dass sich der "
                           f"Auftraggeber fuer Ihr Angebot entschieden hat (Gesamtpreis {total} {offer.currency} netto, "
                           "wie zuletzt angeboten).\n\nDie Beauftragung erfolgt gesondert durch den Auftraggeber; mit dieser "
                           "Nachricht kommt noch kein Vertrag zustande. Wir melden uns zu den naechsten Schritten.\n\n"
                           "Freundliche Gruesse,\nNegotiateX – KI-gestuetzte Beschaffungskoordination.\nTestlauf, keine Beauftragung."),
            status=RFQActionStatus.draft, created_by=str(user.id)))
    others = (await db.execute(select(SupplierCandidate).where(SupplierCandidate.sourcing_request_id == p.sourcing_request_id,
                                                               SupplierCandidate.id != winner.id))).scalars().all()
    declined = 0
    for c in others:
        has_offer = (await db.execute(select(RFQOffer).where(RFQOffer.supplier_candidate_id == c.id))).scalars().first()
        if not has_offer or not c.contact_email:
            continue
        db.add(RFQAction(
            tenant_id=p.tenant_id, rfq_id=has_offer.rfq_id, supplier_candidate_id=c.id, kind="decline_notice",
            recipient_email=c.contact_email, rendered_subject=f"Ihre Angebotsabgabe: {p.title} / {str(c.id)[:8]}",
            rendered_body=("Guten Tag,\n\nvielen Dank fuer Ihr Angebot und die Zeit, die Sie investiert haben. Der Auftraggeber "
                           "hat sich in diesem Fall fuer ein anderes Angebot entschieden. Wir wuerden uns freuen, Sie bei "
                           "kuenftigen Anfragen wieder zu beruecksichtigen.\n\nFreundliche Gruesse,\nNegotiateX – "
                           "KI-gestuetzte Beschaffungskoordination.\nTestlauf, keine Beauftragung."),
            status=RFQActionStatus.draft, created_by=str(user.id)))
        declined += 1
    await add_event(db, p, "awarded", f"Entscheidung: {winner.company_name}",
                    f"Gesamtpreis {total} {offer.currency}. Zusage und {declined} Absage(n) liegen als Entwurf zur Freigabe bereit.",
                    milestone=True, actor=str(user.id))
    await db.commit()
    return await _fresh(db, p)


@router.post("/{project_id}/cancel")
async def cancel(project_id: str, user=Depends(get_current_user), membership=Depends(get_current_membership),
                 db: AsyncSession = Depends(get_db)):
    p = await _get_project(project_id, membership.tenant_id, db)
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
    await add_event(db, p, "cancelled", "Vorhaben abgebrochen", "Offene Entwuerfe wurden verworfen.", milestone=True, actor=str(user.id))
    await db.commit()
    return await _fresh(db, p)


# ---------------------------------------------------------------------------
# Einstieg B: bestehendes Angebot
# ---------------------------------------------------------------------------

@router.post("/from-offer")
async def from_offer(file: UploadFile = File(...), company_name: str = Form(...), contact_email: str = Form(...),
                     contact_name: Optional[str] = Form(None), notes: Optional[str] = Form(None),
                     budget_ceiling: Optional[str] = Form(None), seek_alternatives: bool = Form(True),
                     user=Depends(get_current_user), membership=Depends(get_current_membership),
                     db: AsyncSession = Depends(get_db)):
    """Kunde hat bereits ein Angebot: Bedarf aus dem Angebot ableiten,
    Angebot pruefen, Verhandlung vorbereiten und -- auf Wunsch -- Vergleichs-
    angebote einholen. Das Angebot ist kein Budget; die Obergrenze kommt vom Kunden."""
    from services.briefing import draft_briefing
    from services.offer_intake import store_offer_file, intake_offer
    email = contact_email.strip().lower()
    if "@" not in email:
        raise HTTPException(400, "Bitte eine gueltige E-Mail-Adresse des Dienstleisters angeben.")
    data = await _read_upload(file)
    try:
        doc, _ver, text = await store_offer_file(db, membership.tenant_id, file.filename or "angebot.pdf", data,
                                                 company_name, actor=str(user.id), source="kunde:bestandsangebot")
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not text:
        raise HTTPException(400, "Angebot konnte nicht gelesen werden.")
    combined = text + (f"\n\nHinweise des Auftraggebers:\n{notes}" if notes else "")
    br = await asyncio.to_thread(draft_briefing, combined, "offer")
    p = Project(tenant_id=membership.tenant_id, title=(br.get("title") or f"Angebot {company_name}")[:255],
                entry_path="existing_offer", status=ProjectStatus.evaluating, service_type=br.get("service_type"),
                description=br.get("description") or text[:1500], must_criteria_json=br.get("must_criteria") or {},
                conditions_text=br.get("conditions_text"), currency=br.get("currency") or "EUR",
                needed_by=date.fromisoformat(br["needed_by"]) if br.get("needed_by") else None,
                region=br.get("region"), delivery_location=br.get("delivery_location"),
                quantity=_dec(br.get("quantity")), unit=br.get("unit"), briefing_source="offer",
                briefing_input=(notes or "")[:20000] or None, briefing_document_id=doc.id,
                open_questions_json=br.get("open_questions") or [], seek_alternatives=seek_alternatives,
                budget_ceiling=_dec(budget_ceiling), customer_email=getattr(user, "email", None), created_by=str(user.id))
    db.add(p)
    await db.flush()
    req = await _ensure_request(db, p, str(user.id))
    req.status, req.approved_by, req.approved_at = SourcingRequestStatus.approved, str(user.id), datetime.utcnow()
    rfq = await _ensure_rfq(db, p, req, str(user.id))
    cand = SupplierCandidate(
        tenant_id=p.tenant_id, sourcing_request_id=req.id, company_name=company_name.strip()[:255],
        domain=email.split("@")[-1], service_match_note=p.service_type or p.title, contact_email=email,
        contact_name=contact_name, source_url="kunde:bestandsangebot", retrieved_at=datetime.utcnow(),
        must_criteria_check_json={}, open_questions_json=[], status=CandidateStatus.qualified,
        nda_assessment_json={"needs_nda": False, "reasoning": "Bestehende Geschaeftsbeziehung, Angebot liegt bereits vor.",
                             "assessed_at": datetime.utcnow().isoformat()},
        created_by=str(user.id),
    )
    db.add(cand)
    await db.flush()
    p.incumbent_candidate_id = cand.id
    db.add(RFQInvitation(tenant_id=p.tenant_id, rfq_id=rfq.id, supplier_candidate_id=cand.id, sent_at=datetime.utcnow(),
                         message_id=None))
    await add_event(db, p, "created", "Vorhaben aus bestehendem Angebot angelegt",
                    f"Angebot von {cand.company_name} hochgeladen. Der Agent prueft AGB und Konditionen.", actor=str(user.id))
    offer, review = await intake_offer(db, rfq, cand, text, actor=str(user.id), doc=doc, channel="Kunde", preexisting=True)
    if seek_alternatives:
        p.status = ProjectStatus.sourcing
        await add_event(db, p, "alternatives", "Vergleichsangebote gewuenscht",
                        "Waehlen Sie aus den Vorschlaegen weitere Dienstleister aus; der Agent bereitet die Anfragen vor.",
                        actor=str(user.id))
    await db.commit()
    return {"project": await _fresh(db, p), "offer_id": str(offer.id), "review": review.status, "briefing_warnings": br.get("warnings", [])}
