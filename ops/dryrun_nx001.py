"""
Trockenlauf NX-001 (Pauschalangebote fuer Stueckmengen richtig vergleichen).

Bewertet die beiden Angebote im Vorhaben "Schreibtische" mit der neuen Logik
neu: gespeicherter Angebotstext -> KI-Extraktion (kein tools=) ->
reconcile_prices -> Mengenbeleg -> _comparability -> Pruefung ->
offers_evaluation. Alles in EINER Transaktion, die am Ende per rollback
verworfen wird. Kein Mailversand: services.email_sender.send_negotiation_email
und services.projects.notify_team sind durch Stubs ersetzt. Uploads nach /tmp.

Der Code liegt nicht im Image (COPY app/ ohne ops/). Aufruf auf dem Server:
  docker cp ops/dryrun_nx001.py negotiatex-backend:/tmp/dryrun_nx001.py
  docker exec -w /app -e PYTHONPATH=/app negotiatex-backend python /tmp/dryrun_nx001.py

Vor dem Deploy (Container hat noch den alten Stand) die geaenderten Module
aus dem Branch agent/NX-001 mitgeben; sie werden nur in diesem Prozess geladen:
  docker exec negotiatex-backend mkdir -p /tmp/nx001/services
  docker cp app/services/rfq_classifier.py negotiatex-backend:/tmp/nx001/services/
  docker cp app/services/offer_intake.py   negotiatex-backend:/tmp/nx001/services/
  docker exec -w /app -e PYTHONPATH=/app -e NX001_CODE_DIR=/tmp/nx001 negotiatex-backend python /tmp/dryrun_nx001.py
"""
import asyncio
import importlib.util
import os
import sys
import uuid

os.environ["MDC_UPLOAD_DIR"] = "/tmp/nx001_uploads"

CANDIDATE_IDS = ["41048a16-cb10-45cb-8c91-4d692cc3d96b", "7b590ee8-dc42-442b-a63e-fe45fdcae9ef"]
BEGLEIT = "--- Begleittext der E-Mail ---"
SENT: list = []


def _load_override(name: str) -> None:
    code_dir = os.getenv("NX001_CODE_DIR")
    if not code_dir:
        return
    path = os.path.join(code_dir, "services", f"{name}.py")
    import services
    spec = importlib.util.spec_from_file_location(f"services.{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"services.{name}"] = mod
    spec.loader.exec_module(mod)
    setattr(services, name, mod)
    print(f"[Code] services.{name} aus {path}")


_load_override("rfq_classifier")
_load_override("offer_intake")

import services.email_sender as email_sender  # noqa: E402
import services.projects as projects_service  # noqa: E402


def _no_mail(to_email, subject, body, *a, **kw):
    SENT.append((to_email, subject))
    print(f"[Stub] Mail NICHT versendet an {to_email}: {subject[:80]}")
    return {"sent": False, "message_id": None, "stub": True}


async def _no_team_mail(db, project, title, detail=None, *a, **kw):
    SENT.append(("team", title))
    print(f"[Stub] Team-Benachrichtigung NICHT versendet: {title}")
    return 0


email_sender.send_negotiation_email = _no_mail
projects_service.notify_team = _no_team_mail

from sqlalchemy import select, desc  # noqa: E402

import database  # noqa: E402
from database import AdminSessionLocal, AsyncSessionLocal  # noqa: E402
from models_contracts import RFQ, RFQOffer, OfferStatus  # noqa: E402
from models_mdc import MDCDocument, MDCDocumentVersion  # noqa: E402
from models_projects import OfferReview  # noqa: E402
from models_sourcing import SupplierCandidate  # noqa: E402
import services.offer_intake as oi  # noqa: E402
import services.rfq_classifier as rc  # noqa: E402

if not hasattr(rc, "verify_priced_quantity"):
    sys.exit("Abbruch: services.rfq_classifier hat noch den alten Stand -- NX001_CODE_DIR setzen (siehe Doc).")


def _v(x):
    return getattr(x, "value", x)


async def _offer_text(db, offer: RFQOffer) -> tuple[str, str]:
    """Voller Text: Dokumenttext (MDCDocumentVersion.extracted_text des mit dem
    Angebot verknuepften MDC-Dokuments) + ggf. Begleittext der Mail aus dem
    Pruefungsauszug (OfferReview.offer_text_excerpt, max. 4000 Zeichen)."""
    doc = (await db.execute(select(MDCDocument).where(MDCDocument.rfq_offer_id == offer.id))).scalars().first()
    doc_text = ""
    if doc:
        ver = (await db.execute(select(MDCDocumentVersion).where(MDCDocumentVersion.document_id == doc.id)
                                .order_by(desc(MDCDocumentVersion.version_number)))).scalars().first()
        doc_text = (ver.extracted_text or "") if ver else ""
    rv = (await db.execute(select(OfferReview).where(OfferReview.offer_id == offer.id)
                           .order_by(desc(OfferReview.created_at)))).scalars().first()
    excerpt = (rv.offer_text_excerpt or "") if rv else ""
    if doc_text:
        if BEGLEIT in excerpt:
            return f"{doc_text}\n\n{BEGLEIT}{excerpt.split(BEGLEIT, 1)[1]}", "MDC-Dokument + Begleittext (Auszug)"
        return doc_text, "MDC-Dokument"
    return excerpt, "OfferReview.offer_text_excerpt (max. 4000 Zeichen)"


async def main() -> None:
    async with AdminSessionLocal() as adb:  # nur lesend: Mandant der Kandidaten ermitteln
        tenants = {c.tenant_id for c in (await adb.execute(select(SupplierCandidate).where(
            SupplierCandidate.id.in_([uuid.UUID(c) for c in CANDIDATE_IDS])))).scalars().all()}
    if len(tenants) != 1:
        sys.exit(f"Abbruch: erwartet genau einen Mandanten, gefunden {len(tenants)}.")
    database.current_tenant_id.set(str(tenants.pop()))  # ab hier mit RLS im Mandantenkontext

    async with AsyncSessionLocal() as db:
        try:
            rfq = project = None
            for cid in CANDIDATE_IDS:
                cand = (await db.execute(select(SupplierCandidate).where(SupplierCandidate.id == uuid.UUID(cid)))).scalar_one()
                old = (await db.execute(select(RFQOffer).where(
                    RFQOffer.supplier_candidate_id == cand.id, RFQOffer.status == OfferStatus.submitted,
                ).order_by(desc(RFQOffer.version)))).scalars().first()
                if old is None:
                    print(f"\n### {cand.company_name}: kein aktuelles Angebot -- uebersprungen")
                    continue
                rfq = (await db.execute(select(RFQ).where(RFQ.id == old.rfq_id))).scalar_one()
                text, source = await _offer_text(db, old)
                print(f"\n### {cand.company_name} (Kandidat {cid})")
                print(f"Erwartete Menge (RFQ.expected_quantity): {rfq.expected_quantity}")
                print(f"VORHER  v{old.version}: Menge {old.quantity} · Einzelpreis {old.unit_price} · "
                      f"Summe {oi.offer_total(old)} · {_v(old.comparability_flag)} · {old.comparability_note}")
                print(f"Text: {source}, {len(text)} Zeichen")
                new, review = await oi._intake_offer(db, rfq, cand, text, actor="dryrun-nx001", doc=None,
                                                     channel="Trockenlauf", draft_receipt=False, preexisting=False)
                raw = new.raw_extracted_json or {}
                print(f"NACHHER v{new.version}: Menge {new.quantity} · Einzelpreis {new.unit_price} · "
                      f"Vergleichspreis {oi.offer_total(new)} · {_v(new.comparability_flag)}")
                print(f"  Notiz: {new.comparability_note}")
                print(f"  KI: priced_quantity={raw.get('priced_quantity')} quote={raw.get('priced_quantity_quote')!r} "
                      f"total_price={raw.get('total_price')}")
                print(f"  Beleg gespeichert: lump_sum_quantity={raw.get('lump_sum_quantity')} "
                      f"lump_sum_quote={raw.get('lump_sum_quote')!r}")
                print(f"  Optionale Positionen (belegt): {raw.get('optional_items_verified')}")
                for f in review.findings_json or []:
                    if f.get("code") in ("optional_item", "price_extraction", "not_comparable"):
                        print(f"  Hinweis [{f.get('severity')}] {f.get('code')}: {f.get('message')}"
                              + (f"  Zitat: „{f['quote']}“" if f.get("quote") else ""))
                project = await oi.project_for_rfq(db, rfq)
            if rfq is not None:
                print("\n### Auswertung (offers_evaluation)")
                print(await oi.offers_evaluation(db, rfq, project))
                offers = (await db.execute(select(RFQOffer).where(RFQOffer.rfq_id == rfq.id))).scalars().all()
                cmp_ = rc.compute_offer_comparison(rfq, offers, {})
                print("\n### Rangliste compute_offer_comparison")
                for r in cmp_["ranked_comparable"]:
                    print(f"  {r['rank']}. Kandidat {r['supplier_candidate_id'][:8]} v{r['version']}: {r['total_cost_net']}")
        finally:
            await db.rollback()
            print(f"\nROLLBACK ausgefuehrt. Abgefangene Mails: {len(SENT)} (keine versendet).")


if __name__ == "__main__":
    asyncio.run(main())
