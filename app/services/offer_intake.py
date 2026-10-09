"""
Angebotseingang -- ein Weg fuer alle Quellen (E-Mail-Anhang, Upload im
Vorhaben, Bestandsangebot beim Einstieg "Ich habe schon ein Angebot").

Ablauf:
  1. Datei unveraendert im Master Data Center ablegen (Dokumenttyp 'offer',
     Hash-Duplikaterkennung, Text lesen) -- das Original bleibt nachweisbar.
  2. Angebotsfelder per KI extrahieren (kein `tools=`, fehlende Felder bleiben
     leer, nichts wird geraten) und als RFQOffer speichern. Ein erneutes
     Angebot desselben Bieters wird als neue Version gefuehrt, die alte als
     'superseded' markiert.
  3. Vergleichbarkeit (deterministisch) + AGB-/Compliance-Pruefung
     (services/offer_compliance) -> OfferReview.
  4. Eingangsbestaetigung als ENTWURF (Versand erst nach Freigabe),
     Zeitstrahl-Eintrag + Meilenstein-Mail an den Kunden.
"""
import asyncio
import hashlib
import logging
import os
import tempfile
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from models_contracts import (
    RFQ, RFQStatus, RFQInvitation, RFQAction, RFQActionStatus, RFQOffer, OfferStatus, ComparabilityFlag,
)
from models_mdc import MDCDocument, MDCDocumentVersion, MDCDocumentType, MDCImportStatus, MDCSupplier
from models_projects import Project, ProjectStatus, OfferReview
from models_sourcing import SupplierCandidate, OutreachMessage, OutreachDirection, OutreachReminderTimer

logger = logging.getLogger(__name__)

UPLOAD_DIR = Path(os.getenv("MDC_UPLOAD_DIR", "/app/uploads/mdc"))
OFFER_EXTENSIONS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv"}


def _dec(v) -> Optional[Decimal]:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float, Decimal)):
        return Decimal(str(v))
    s = str(v).strip().replace("EUR", "").replace("€", "").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def _in_text(v: Optional[Decimal], text: str) -> bool:
    """Ganzzahliger Teil des Betrags muss als Ziffernfolge im Text stehen
    (1.234,00 / 1234.00 / 1 234 -> 1234) -- Schutz gegen erfundene Zahlen."""
    import re
    if v is None:
        return False
    digits = re.sub(r"\D", "", str(int(v)))
    return bool(digits) and digits in re.sub(r"\D", "", text or "")


def reconcile_prices(extracted: dict, text: str) -> dict:
    """Bringt Einzelpreis, Menge, Zusatzkosten und ausdruecklich genannten
    Gesamtpreis in Einklang. Massgeblich ist im Zweifel der im Dokument
    stehende Gesamtpreis; Betraege ohne Beleg im Text werden verworfen."""
    up, qty = _dec(extracted.get("unit_price")), _dec(extracted.get("quantity"))
    fr, oc = _dec(extracted.get("freight_cost")), _dec(extracted.get("other_costs"))
    tp = _dec(extracted.get("total_price"))
    notes = []
    for name, val in (("Gesamtpreis", tp), ("Einzelpreis", up)):
        if val is not None and not _in_text(val, text):
            notes.append(f"{name} {val} nicht im Dokument belegt -- verworfen.")
    tp = tp if _in_text(tp, text) else None
    up = up if _in_text(up, text) else None
    extras = (fr or Decimal("0")) + (oc or Decimal("0"))
    if tp is not None:
        q = qty or Decimal("1")
        if up is None:
            up, qty = tp, Decimal("1")
            if extras == tp:  # Gesamtsumme faelschlich als Zusatzkosten erfasst
                fr = oc = Decimal("0")
        elif abs(up * q + extras - tp) > Decimal("0.01") and abs(up * q - tp) > Decimal("0.01"):
            notes.append(f"Einzelpositionen ergeben nicht den genannten Gesamtpreis {tp} -- Gesamtpreis uebernommen.")
            up, qty, fr, oc = tp, Decimal("1"), Decimal("0"), Decimal("0")
        elif abs(up * q + extras - tp) <= Decimal("0.01") and extras:
            # Zusatzkosten sind bereits im Gesamtpreis enthalten -> nicht doppelt zaehlen
            up, qty, fr, oc = tp, Decimal("1"), Decimal("0"), Decimal("0")
    out = dict(extracted)
    out.update({"unit_price": up, "quantity": qty, "freight_cost": fr, "other_costs": oc, "total_price": tp,
                "price_notes": notes})
    return out


def _date(v) -> Optional[datetime]:
    if not v:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(str(v).strip()[:10], fmt)
        except ValueError:
            continue
    return None


async def store_offer_file(db: AsyncSession, tenant_id, filename: str, data: bytes, supplier_name: Optional[str],
                           category_id=None, actor: str = "system", source: str = "email") -> tuple[MDCDocument, MDCDocumentVersion, str]:
    """Legt die Datei als MDC-Dokument (Typ offer) ab und liefert den Text.
    Exakt gleiche Datei (Hash) beim gleichen Mandanten -> bestehende Version."""
    from services.pdf_parser import extract_text
    suffix = Path(filename or "").suffix.lower()
    if suffix not in OFFER_EXTENSIONS:
        raise ValueError(f"Dateityp {suffix or '?'} wird nicht unterstuetzt.")
    file_hash = hashlib.sha256(data).hexdigest()
    dup = (await db.execute(select(MDCDocumentVersion).where(
        MDCDocumentVersion.tenant_id == tenant_id, MDCDocumentVersion.file_hash == file_hash))).scalar_one_or_none()
    if dup:
        doc = (await db.execute(select(MDCDocument).where(MDCDocument.id == dup.document_id))).scalar_one()
        return doc, dup, dup.extracted_text or ""

    supplier_id = None
    if supplier_name:
        s = (await db.execute(select(MDCSupplier).where(MDCSupplier.tenant_id == tenant_id,
                                                         MDCSupplier.name == supplier_name.strip()))).scalar_one_or_none()
        if not s:
            s = MDCSupplier(tenant_id=tenant_id, name=supplier_name.strip())
            db.add(s)
            await db.flush()
        supplier_id = s.id

    doc = MDCDocument(tenant_id=tenant_id, category_id=category_id, supplier_id=supplier_id,
                      document_type=MDCDocumentType.offer, title=filename, created_by=str(actor),
                      usage_purpose="Angebotsbewertung im Vorhaben")
    db.add(doc)
    await db.flush()
    tenant_dir = UPLOAD_DIR / str(tenant_id)
    tenant_dir.mkdir(parents=True, exist_ok=True)
    stored = tenant_dir / f"{file_hash}{suffix}"
    if not stored.exists():
        stored.write_bytes(data)
    version = MDCDocumentVersion(tenant_id=tenant_id, document_id=doc.id, version_number=1, file_name=filename,
                                 file_path=str(stored), file_hash=file_hash, file_size_bytes=len(data), source=source,
                                 import_status=MDCImportStatus.uploaded, created_by=str(actor))
    db.add(version)
    await db.flush()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        text = await extract_text(tmp_path, filename)
    finally:
        os.unlink(tmp_path)
    if text and not text.startswith("[Error") and not text.startswith("[Unsupported"):
        version.extracted_text = text
        version.import_status = MDCImportStatus.parsed
    else:
        version.import_error = text or "Kein Text extrahiert."
        version.import_status = MDCImportStatus.needs_review
        text = ""
    from services.mdc_governance import audit
    await audit(db, tenant_id, actor, "document_uploaded", "document_version", version.id,
                document_id=str(doc.id), version_number=1, file_hash=file_hash, source=source)
    return doc, version, text


async def project_for_rfq(db: AsyncSession, rfq: RFQ) -> Optional[Project]:
    p = (await db.execute(select(Project).where(Project.rfq_id == rfq.id))).scalars().first()
    if p:
        return p
    return (await db.execute(select(Project).where(Project.sourcing_request_id == rfq.sourcing_request_id))).scalars().first()


async def run_review(db: AsyncSession, offer: RFQOffer, project: Optional[Project], offer_text: str, actor: str = "agent") -> OfferReview:
    from services.offer_compliance import review
    res = await asyncio.to_thread(review, offer, project, offer_text or "")
    rv = OfferReview(tenant_id=offer.tenant_id, offer_id=offer.id, project_id=project.id if project else None,
                     status=res["status"],
                     findings_json=res["findings"], offer_text_excerpt=(offer_text or "")[:4000],
                     rules_version=res["rules_version"], created_by=str(actor))
    db.add(rv)
    await db.flush()
    return rv


async def intake_offer(db: AsyncSession, rfq: RFQ, cand: SupplierCandidate, offer_text: str, actor: str = "agent",
                       doc: Optional[MDCDocument] = None, channel: str = "E-Mail", draft_receipt: bool = True,
                       preexisting: bool = False) -> tuple[RFQOffer, OfferReview]:
    from services.rfq_classifier import extract_offer_fields, _comparability
    extracted = await asyncio.to_thread(extract_offer_fields, offer_text) if offer_text else {}
    extracted = reconcile_prices(extracted, offer_text or "")

    previous = (await db.execute(select(RFQOffer).where(
        RFQOffer.rfq_id == rfq.id, RFQOffer.supplier_candidate_id == cand.id, RFQOffer.status == OfferStatus.submitted,
    ).order_by(desc(RFQOffer.version)))).scalars().first()

    offer = RFQOffer(
        tenant_id=rfq.tenant_id, rfq_id=rfq.id, supplier_candidate_id=cand.id,
        version=(previous.version + 1) if previous else 1,
        unit_price=_dec(extracted.get("unit_price")), quantity=_dec(extracted.get("quantity")),
        freight_cost=_dec(extracted.get("freight_cost")) or Decimal("0"), other_costs=_dec(extracted.get("other_costs")) or Decimal("0"),
        currency=(extracted.get("currency") or rfq.currency or "EUR")[:10],
        delivery_date=(str(extracted["delivery_date"])[:100] if extracted.get("delivery_date") else None),
        payment_terms=extracted.get("payment_terms"), offer_validity_until=_date(extracted.get("offer_validity_until")),
        scope_note=extracted.get("scope_note"), spec_confirmed=extracted.get("spec_confirmed"),
        raw_extracted_json={**{k: (v if isinstance(v, list) or v is None else str(v)) for k, v in extracted.items()}, "channel": channel},
        created_by=str(actor),
    )
    flag, note = _comparability(offer.quantity, rfq.expected_quantity, offer.scope_note, offer.spec_confirmed)
    offer.comparability_flag = ComparabilityFlag(flag)
    offer.comparability_note = note
    db.add(offer)
    await db.flush()
    if previous:
        previous.status = OfferStatus.superseded
        previous.superseded_by_id = offer.id
    if doc is not None:
        doc.rfq_offer_id = offer.id
    if rfq.status in (RFQStatus.draft, RFQStatus.sent):
        rfq.status = RFQStatus.collecting_offers

    if draft_receipt and cand.contact_email and not preexisting:
        db.add(RFQAction(
            tenant_id=rfq.tenant_id, rfq_id=rfq.id, supplier_candidate_id=cand.id, kind="receipt_confirmation",
            recipient_email=cand.contact_email,
            rendered_subject=f"Eingangsbestaetigung Ihres Angebots / {str(cand.id)[:8]}",
            rendered_body=(f"Guten Tag,\n\nwir bestaetigen den Eingang Ihres Angebots (Vorgang {str(rfq.id)[:8]}).\n"
                           "Wir pruefen es im Rahmen der laufenden Angebotsfrist und melden uns.\n\n"
                           "Freundliche Gruesse,\nNegotiateX – KI-gestuetzte Beschaffungsassistenz."),
            status=RFQActionStatus.draft, created_by=str(actor),
        ))

    project = await project_for_rfq(db, rfq)
    review = await run_review(db, offer, project, offer_text, actor)
    if extracted.get("price_notes"):
        review.findings_json = list(review.findings_json or []) + [
            {"code": "price_extraction", "source": "regel", "severity": "info", "message": n, "quote": None}
            for n in extracted["price_notes"]]

    if project:
        from services.projects import add_event
        crit = sum(1 for f in review.findings_json or [] if f.get("severity") == "critical")
        warn = sum(1 for f in review.findings_json or [] if f.get("severity") == "warning")
        what = "Bestehendes Angebot erfasst" if preexisting else f"Angebot von {cand.company_name} eingegangen"
        if offer.version > 1:
            what = f"Neue Angebotsversion {offer.version} von {cand.company_name}"
        detail = f"Pruefung: {crit} kritische Punkte, {warn} Hinweise." if (crit or warn) else "Pruefung ohne Auffaelligkeiten."
        await add_event(db, project, "offer_received", what, detail, milestone=True)
        if project.status in (ProjectStatus.sourcing, ProjectStatus.collecting_offers, ProjectStatus.briefing):
            project.status = ProjectStatus.evaluating
    return offer, review


async def _latest_sent_invitation(db: AsyncSession, cand: SupplierCandidate) -> Optional[RFQInvitation]:
    return (await db.execute(select(RFQInvitation).where(
        RFQInvitation.supplier_candidate_id == cand.id, RFQInvitation.sent_at.isnot(None),
    ).order_by(desc(RFQInvitation.sent_at)))).scalars().first()


async def intake_from_email(db: AsyncSession, m: dict, cand: SupplierCandidate) -> Optional[dict]:
    """Antwort eines eingeladenen Bieters mit Angebotsanhang. Rueckgabe None,
    wenn die Mail kein Angebot im Sinne dieses Pfades ist (dann greift die
    normale Outreach-Verarbeitung)."""
    attachments = [a for a in (m.get("attachments") or []) if Path(a["filename"]).suffix.lower() in OFFER_EXTENSIONS]
    if not attachments:
        return None
    inv = await _latest_sent_invitation(db, cand)
    if not inv:
        return None
    rfq = (await db.execute(select(RFQ).where(RFQ.id == inv.rfq_id))).scalar_one()

    if m.get("message_id"):
        if (await db.execute(select(OutreachMessage).where(OutreachMessage.message_id == m["message_id"]))).scalar_one_or_none():
            return {"matched": True, "duplicate": True}
    db.add(OutreachMessage(
        tenant_id=cand.tenant_id, supplier_candidate_id=cand.id, direction=OutreachDirection.inbound,
        message_id=m.get("message_id") or f"<generated-{uuid.uuid4()}@negotiatex.ai>",
        in_reply_to=m.get("in_reply_to"), references_header=m.get("references_header"),
        from_addr=(m.get("from_addr") or "")[:255], to_addr=(m.get("to_addr") or "info@negotiatex.ai")[:255],
        subject=m.get("subject"), body_text=m.get("body_text") or "", raw_source=m.get("raw_source"),
    ))
    for t in (await db.execute(select(OutreachReminderTimer).where(
            OutreachReminderTimer.supplier_candidate_id == cand.id, OutreachReminderTimer.cancelled == False,  # noqa: E712
            OutreachReminderTimer.fired == False))).scalars().all():  # noqa: E712
        t.cancelled = True

    project = await project_for_rfq(db, rfq)
    results = []
    for att in attachments[:1]:  # ein Angebot pro Mail; weitere Anhaenge bleiben im Rohtext nachvollziehbar
        doc, version, text = await store_offer_file(db, cand.tenant_id, att["filename"], att["data"], cand.company_name,
                                                    category_id=project.category_id if project else None, source="email")
        full_text = f"{text}\n\n--- Begleittext der E-Mail ---\n{m.get('body_text') or ''}" if text else (m.get("body_text") or "")
        offer, review = await intake_offer(db, rfq, cand, full_text, actor="agent", doc=doc, channel="E-Mail")
        results.append({"offer_id": str(offer.id), "version": offer.version, "review": review.status, "document_id": str(doc.id)})
    await db.commit()
    return {"matched": True, "kind": "offer", "offers": results}
