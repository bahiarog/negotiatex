"""
Master Data Center Etappe 4 -- Betriebskennzahlen und Zustand.

GET /metrics -- die Betriebsmetriken der Anleitung (Abschnitt 15) je
Mandant: Anteil pruefbarer Positionen, offene Pflichtfelder,
Quellenabdeckung, Korrekturen/Ablehnungen, Datenalter, Datenbreite je
Kategorie, Extraktions-Latenz, Rechte-Integritaet, Tokens je Dokument.
HTTP-Latenzen aller Endpunkte (inkl. Analyse) liegen zusaetzlich in
Prometheus/Grafana (prometheus-fastapi-instrumentator).

GET /health -- technischer Zustand fuer Betrieb (DB, pgvector, Modell,
Index-Konsistenz).
"""
import os
from collections import Counter, defaultdict
from datetime import date
from statistics import median

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from deps import get_current_membership
from models_mdc import (
    MDCAuditEvent, MDCCategory, MDCDocument, MDCDocumentVersion, MDCImportStatus, MDCLineItem, MDCReviewStatus,
)
from services.mdc_analytics import POLICIES, DEFAULT_POLICY_ID
from services.mdc_governance import usage_block_reason

router = APIRouter()


def _pct(a: int, b: int):
    return round(100 * a / b, 1) if b else None


def _ev(v):
    return v.value if hasattr(v, "value") else v


@router.get("/metrics")
async def mdc_metrics(membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    tid = membership.tenant_id
    today = date.today()
    rows = (await db.execute(
        select(MDCLineItem, MDCDocumentVersion, MDCDocument)
        .join(MDCDocumentVersion, MDCLineItem.document_version_id == MDCDocumentVersion.id)
        .join(MDCDocument, MDCDocumentVersion.document_id == MDCDocument.id)
        .where(MDCLineItem.tenant_id == tid)
    )).all()
    active = [(i, v, d) for i, v, d in rows if i.review_status != MDCReviewStatus.superseded]
    status_counts = Counter(_ev(i.review_status) for i, _, _ in active)
    approved = [(i, v, d) for i, v, d in active if i.review_status == MDCReviewStatus.approved]

    issue_codes = Counter(x.get("code") for i, _, _ in active if i.review_status == MDCReviewStatus.needs_review
                          for x in (i.open_issues_json or []) if x.get("blocking"))

    evidence_found = sum(1 for i, v, _ in approved if i.source_evidence and i.source_evidence.strip() in (v.extracted_text or ""))
    usable_approved = [(i, v, d) for i, v, d in approved if not usage_block_reason(d, today)]

    ages = []
    expired_validity = 0
    for i, _, _ in usable_approved:
        ref = i.valid_from or i.offer_date
        if ref:
            ages.append((today - ref).days)
        if i.valid_to and i.valid_to < today:
            expired_validity += 1
    freshness = POLICIES[DEFAULT_POLICY_ID]["freshness_days"]

    corrections_after_approval = (await db.execute(select(func.count()).select_from(MDCAuditEvent).where(
        MDCAuditEvent.tenant_id == tid, MDCAuditEvent.action == "line_item_updated",
        MDCAuditEvent.details_json["previous_status"].as_string() == "approved"))).scalar()
    review_requests = (await db.execute(select(func.count()).select_from(MDCAuditEvent).where(
        MDCAuditEvent.tenant_id == tid, MDCAuditEvent.action == "review_requested"))).scalar()

    cats = {c.id: c.name for c in (await db.execute(select(MDCCategory).where(MDCCategory.tenant_id == tid))).scalars().all()}
    breadth: dict = defaultdict(lambda: defaultdict(lambda: {"observations": 0, "projects": set(), "suppliers": set()}))
    for i, v, d in usable_approved:
        b = breadth[cats.get(i.category_id, "ohne Kategorie")][_ev(i.price_status)]
        b["observations"] += 1
        b["projects"].add(d.id)
        if i.supplier_id:
            b["suppliers"].add(i.supplier_id)
    policy = POLICIES[DEFAULT_POLICY_ID]
    breadth_out = {
        cat: {cls: {"observations": x["observations"], "projects": len(x["projects"]), "suppliers": len(x["suppliers"]),
                    "benchmark_eligible": len(x["projects"]) >= policy["benchmark_min_projects"] and len(x["suppliers"]) >= policy["benchmark_min_suppliers"]}
              for cls, x in classes.items()}
        for cat, classes in breadth.items()
    }

    versions = (await db.execute(select(MDCDocumentVersion).where(
        MDCDocumentVersion.tenant_id == tid, MDCDocumentVersion.extraction_seconds.isnot(None)))).scalars().all()
    secs = sorted(float(v.extraction_seconds) for v in versions)
    in_tok = [v.extraction_input_tokens for v in versions if v.extraction_input_tokens is not None]
    out_tok = [v.extraction_output_tokens for v in versions if v.extraction_output_tokens is not None]
    price_in, price_out = os.getenv("MDC_PRICE_INPUT_PER_MTOK"), os.getenv("MDC_PRICE_OUTPUT_PER_MTOK")
    cost = None
    if price_in and price_out and in_tok and out_tok:
        cost = round((sum(in_tok) * float(price_in) + sum(out_tok) * float(price_out)) / 1_000_000 / len(versions), 4)

    docs = (await db.execute(select(MDCDocument).where(MDCDocument.tenant_id == tid))).scalars().all()
    blocked_docs = [d for d in docs if usage_block_reason(d, today)]
    leaked_chunks = 0
    if blocked_docs:
        leaked_chunks = (await db.execute(sql_text("""
            SELECT count(*) FROM mdc_retrieval_chunks c JOIN mdc_document_versions v ON v.id = c.document_version_id
            WHERE v.document_id = ANY(CAST(:ids AS uuid[]))"""), {"ids": [str(d.id) for d in blocked_docs]})).scalar()

    total = len(active)
    return {
        "as_of": today.isoformat(),
        "positions": {"total": total, "by_status": dict(status_counts),
                      "approved_share_pct": _pct(len(approved), total)},
        "open_mandatory_fields": {"positions": status_counts.get("needs_review", 0), "by_issue": dict(issue_codes)},
        "source_coverage": {"approved_with_evidence_in_text_pct": _pct(evidence_found, len(approved)),
                            "approved_usable_pct": _pct(len(usable_approved), len(approved))},
        "corrections": {"rejected": status_counts.get("rejected", 0),
                        "changed_after_approval": corrections_after_approval, "review_requests": review_requests},
        "data_age": {"median_days": median(ages) if ages else None,
                     f"older_than_{freshness}_days_pct": _pct(sum(1 for a in ages if a > freshness), len(ages)),
                     "validity_expired": expired_validity},
        "breadth_by_category": breadth_out,
        "extraction": {"versions": len(versions), "median_seconds": round(median(secs), 2) if secs else None,
                       "p95_seconds": secs[min(len(secs) - 1, int(0.95 * len(secs)))] if secs else None,
                       "avg_input_tokens": round(sum(in_tok) / len(in_tok)) if in_tok else None,
                       "avg_output_tokens": round(sum(out_tok) / len(out_tok)) if out_tok else None,
                       "avg_cost_per_document": cost,
                       "cost_note": None if cost is not None else "Preise je Mio. Tokens nicht konfiguriert (MDC_PRICE_INPUT_PER_MTOK/_OUTPUT_)."},
        "rights": {"documents": len(docs), "blocked_documents": len(blocked_docs),
                   "search_chunks_of_blocked_documents": leaked_chunks,
                   "legal_hold": sum(1 for d in docs if d.legal_hold)},
    }


@router.get("/health")
async def mdc_health(membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    from services.mdc_embeddings import MODEL_NAME, _model
    checks = {}
    checks["database"] = (await db.execute(sql_text("SELECT 1"))).scalar() == 1
    checks["pgvector"] = (await db.execute(sql_text("SELECT extversion FROM pg_extension WHERE extname = 'vector'"))).scalar()
    checks["embedding_model"] = MODEL_NAME
    checks["embedding_model_loaded_in_this_worker"] = _model is not None
    idx = (await db.execute(sql_text("""
        SELECT count(*) FILTER (WHERE c.embedding IS NULL) AS without_vector,
               count(*) FILTER (WHERE c.embedding_model IS DISTINCT FROM :m AND c.embedding IS NOT NULL) AS other_model,
               count(*) AS total
        FROM mdc_retrieval_chunks c WHERE c.tenant_id = :t"""), {"m": MODEL_NAME, "t": str(membership.tenant_id)})).one()
    checks["search_index"] = {"chunks": idx.total, "without_vector": idx.without_vector, "other_model": idx.other_model}
    unindexed = (await db.execute(select(func.count()).select_from(MDCDocumentVersion).where(
        MDCDocumentVersion.tenant_id == membership.tenant_id, MDCDocumentVersion.import_status == MDCImportStatus.approved))).scalar()
    checks["approved_but_not_indexed_versions"] = unindexed
    ok = checks["database"] and bool(checks["pgvector"]) and idx.without_vector == 0 and idx.other_model == 0
    return {"status": "ok" if ok else "degraded", "checks": checks}
