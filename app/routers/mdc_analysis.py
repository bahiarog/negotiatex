"""
Master Data Center Etappe 3 -- Analyse und Belegzugriff.

Die "Agenten-Tools" der Anleitung (Abschnitt 11) sind hier als eng
begrenzte, serverseitig berechtigte Endpunkte umgesetzt:
  compare_offer            POST /analyses
  get_analysis_snapshot    GET  /analyses/{id}
  get_evidence             GET  /evidence/{line_item_id}
  search_reference_context GET  /search
  request_data_review      POST /line-items/{id}/request-review

Kein Sprachmodell bekommt Werkzeuge oder Lesezugriff auf den Bestand (gleiche
Linie wie Teil A-C): die Zahlen kommen ausschliesslich aus
services/mdc_analytics, die Erklaerung ist ein Textbaustein daraus. Der
Mandant kommt immer aus der Session -- eine mitgesendete fremde ID fuehrt
zu 404, nie zu Daten (zusaetzlich zu RLS).
"""
import asyncio
import logging
import uuid
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, desc, text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from deps import get_current_user, get_current_membership
from models_mdc import (
    MDCLineItem, MDCDocumentVersion, MDCDocument, MDCSupplier, MDCAnalysisSnapshot,
    MDCReviewStatus, MDCImportStatus, MDCDocumentType,
)
from services.mdc_analytics import compare, explain, POLICIES, DEFAULT_POLICY_ID

router = APIRouter()
logger = logging.getLogger(__name__)


def _ev(v):
    return v.value if hasattr(v, "value") else v


def _iso(v):
    return v.isoformat() if isinstance(v, (date, datetime)) else v


def _item_dict(item: MDCLineItem, version: MDCDocumentVersion, doc: MDCDocument, supplier: Optional[MDCSupplier]) -> dict:
    return {
        "id": str(item.id), "document_id": str(doc.id), "document_title": doc.title,
        "version_number": version.version_number,
        "category_id": str(item.category_id) if item.category_id else None,
        "supplier_id": str(item.supplier_id) if item.supplier_id else None,
        "supplier_name": supplier.name if supplier else None,
        "role_or_item": item.role_or_item, "seniority": item.seniority, "region": item.region,
        "canonical_unit": item.canonical_unit, "original_currency": item.original_currency,
        "normalized_amount_per_canonical_unit": str(item.normalized_amount_per_canonical_unit) if item.normalized_amount_per_canonical_unit is not None else None,
        "price_status": _ev(item.price_status), "review_status": _ev(item.review_status),
        "offer_date": _iso(item.offer_date), "valid_from": _iso(item.valid_from), "valid_to": _iso(item.valid_to),
        "ancillary_costs_json": item.ancillary_costs_json or {}, "open_issues_json": item.open_issues_json or [],
        "source_evidence": item.source_evidence, "created_at": _iso(item.created_at),
    }


async def _load_item(db: AsyncSession, item_id, tenant_id):
    row = (await db.execute(
        select(MDCLineItem, MDCDocumentVersion, MDCDocument, MDCSupplier)
        .join(MDCDocumentVersion, MDCLineItem.document_version_id == MDCDocumentVersion.id)
        .join(MDCDocument, MDCDocumentVersion.document_id == MDCDocument.id)
        .outerjoin(MDCSupplier, MDCLineItem.supplier_id == MDCSupplier.id)
        .where(MDCLineItem.id == item_id, MDCLineItem.tenant_id == tenant_id)
    )).first()
    if not row:
        raise HTTPException(404, "Position nicht gefunden.")
    return row


def _snapshot_to_dict(s: MDCAnalysisSnapshot) -> dict:
    return {
        "id": str(s.id), "target_line_item_id": str(s.target_line_item_id) if s.target_line_item_id else None,
        "rfq_offer_id": str(s.rfq_offer_id) if s.rfq_offer_id else None,
        "as_of": _iso(s.as_of), "policy_id": s.policy_id, "policy_version": s.policy_version, "purpose": s.purpose,
        "status": s.status, "input": s.input_json, "result": s.result_json, "explanation": s.explanation,
        "created_by": s.created_by, "created_at": _iso(s.created_at),
    }


async def run_offer_analysis(db: AsyncSession, tenant_id, target_item_id, as_of: date, policy_id: str,
                             purpose: str, user_id: str, rfq_offer_id=None) -> MDCAnalysisSnapshot:
    """compare_offer. Referenzen: nur APPROVED-Positionen derselben
    Kategorie desselben Mandanten aus nicht ersetzten Dokumentversionen."""
    if policy_id not in POLICIES:
        raise HTTPException(400, f"Unbekannte Policy. Verfuegbar: {sorted(POLICIES)}.")
    item, version, doc, supplier = await _load_item(db, target_item_id, tenant_id)
    if item.review_status in (MDCReviewStatus.superseded, MDCReviewStatus.rejected):
        raise HTTPException(400, f"Position im Status '{_ev(item.review_status)}' kann nicht analysiert werden.")
    target = _item_dict(item, version, doc, supplier)

    candidates = []
    if item.category_id:
        rows = (await db.execute(
            select(MDCLineItem, MDCDocumentVersion, MDCDocument, MDCSupplier)
            .join(MDCDocumentVersion, MDCLineItem.document_version_id == MDCDocumentVersion.id)
            .join(MDCDocument, MDCDocumentVersion.document_id == MDCDocument.id)
            .outerjoin(MDCSupplier, MDCLineItem.supplier_id == MDCSupplier.id)
            .where(
                MDCLineItem.tenant_id == tenant_id, MDCLineItem.category_id == item.category_id,
                MDCLineItem.review_status == MDCReviewStatus.approved,
                MDCDocumentVersion.import_status != MDCImportStatus.superseded,
            )
        )).all()
        candidates = [_item_dict(*r) for r in rows]

    result = compare(target, candidates, as_of, policy_id)
    policy = POLICIES[policy_id]
    snapshot = MDCAnalysisSnapshot(
        tenant_id=tenant_id, target_line_item_id=item.id, rfq_offer_id=rfq_offer_id, as_of=as_of,
        policy_id=policy_id, policy_version=policy["version"], purpose=purpose, status=result["status"],
        input_json={"target": target, "candidate_line_item_ids": sorted(c["id"] for c in candidates)},
        result_json=result, explanation=explain(result), created_by=user_id,
    )
    db.add(snapshot)
    await db.flush()
    return snapshot


class AnalysisRequest(BaseModel):
    line_item_id: uuid.UUID
    as_of: Optional[date] = None
    policy_id: str = DEFAULT_POLICY_ID
    purpose: str = "internal_offer_review"


@router.post("/analyses")
async def create_analysis(payload: AnalysisRequest, user=Depends(get_current_user),
                          membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    snapshot = await run_offer_analysis(db, membership.tenant_id, payload.line_item_id, payload.as_of or date.today(),
                                        payload.policy_id, payload.purpose[:100], str(user.id))
    await db.commit()
    await db.refresh(snapshot)
    return _snapshot_to_dict(snapshot)


@router.get("/analyses")
async def list_analyses(line_item_id: Optional[uuid.UUID] = None, rfq_offer_id: Optional[uuid.UUID] = None,
                        membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    q = select(MDCAnalysisSnapshot).where(MDCAnalysisSnapshot.tenant_id == membership.tenant_id)
    if line_item_id:
        q = q.where(MDCAnalysisSnapshot.target_line_item_id == line_item_id)
    if rfq_offer_id:
        q = q.where(MDCAnalysisSnapshot.rfq_offer_id == rfq_offer_id)
    rows = (await db.execute(q.order_by(desc(MDCAnalysisSnapshot.created_at)).limit(200))).scalars().all()
    return [{
        "id": str(s.id), "target_line_item_id": str(s.target_line_item_id) if s.target_line_item_id else None,
        "rfq_offer_id": str(s.rfq_offer_id) if s.rfq_offer_id else None, "as_of": _iso(s.as_of),
        "policy_id": s.policy_id, "status": s.status, "target": (s.input_json or {}).get("target", {}),
        "primary_class": (s.result_json or {}).get("primary_class"), "created_at": _iso(s.created_at),
    } for s in rows]


@router.get("/analyses/{snapshot_id}")
async def get_analysis(snapshot_id: uuid.UUID, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    s = (await db.execute(select(MDCAnalysisSnapshot).where(
        MDCAnalysisSnapshot.id == snapshot_id, MDCAnalysisSnapshot.tenant_id == membership.tenant_id,
    ))).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "Analyse nicht gefunden.")
    return _snapshot_to_dict(s)


@router.get("/offer-targets")
async def list_offer_targets(membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """Positionen aus Angebotsdokumenten, die verglichen werden koennen."""
    rows = (await db.execute(
        select(MDCLineItem, MDCDocumentVersion, MDCDocument, MDCSupplier)
        .join(MDCDocumentVersion, MDCLineItem.document_version_id == MDCDocumentVersion.id)
        .join(MDCDocument, MDCDocumentVersion.document_id == MDCDocument.id)
        .outerjoin(MDCSupplier, MDCLineItem.supplier_id == MDCSupplier.id)
        .where(
            MDCLineItem.tenant_id == membership.tenant_id, MDCDocument.document_type == MDCDocumentType.offer,
            MDCLineItem.review_status.in_([MDCReviewStatus.extracted, MDCReviewStatus.needs_review, MDCReviewStatus.approved]),
            MDCDocumentVersion.import_status != MDCImportStatus.superseded,
        ).order_by(desc(MDCLineItem.created_at))
    )).all()
    return [_item_dict(*r) for r in rows]


@router.get("/evidence/{line_item_id}")
async def get_evidence(line_item_id: uuid.UUID, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """Exakt die zugelassene Belegstelle einer Position plus wenige Zeilen
    Kontext -- keine frei waehlbaren Dateipfade."""
    item, version, doc, supplier = await _load_item(db, line_item_id, membership.tenant_id)
    evidence = (item.source_evidence or "").strip()
    lines = (version.extracted_text or "").splitlines()
    context, found = [], False
    if evidence:
        for idx, line in enumerate(lines):
            if evidence in line or line.strip() and line.strip() in evidence:
                context = lines[max(0, idx - 2): idx + 3]
                found = True
                break
    return {
        "line_item_id": str(item.id), "document_id": str(doc.id), "document_title": doc.title,
        "document_version_id": str(version.id), "version_number": version.version_number,
        "version_status": _ev(version.import_status), "supplier_name": supplier.name if supplier else None,
        "source_evidence": evidence or None, "evidence_found_in_text": found, "context": context,
        "download_path": f"/api/v1/mdc/documents/{doc.id}/versions/{version.id}/download",
    }


SEARCH_CANDIDATES = 30
RRF_K = 60
# Kalibriert 09.10.2026 auf zeilengenauen Abschnitten (lines-v2): relevante
# Treffer 0.27-0.53 (Median 0.40), sinnlose Anfragen hoechstens 0.24. Auf dem
# kleinen Testbestand ermittelt -- mit echten Daten neu pruefen.
SEMANTIC_MIN_SIMILARITY = 0.26

_SEARCH_SCOPE = """
        FROM mdc_retrieval_chunks c
        JOIN mdc_document_versions v ON v.id = c.document_version_id
        JOIN mdc_documents d ON d.id = v.document_id
        WHERE c.tenant_id = :tenant_id
          AND v.import_status = 'indexed'
          AND (CAST(:category_id AS uuid) IS NULL OR d.category_id = CAST(:category_id AS uuid))
"""


@router.get("/search")
async def search_reference_context(q: str, category_id: Optional[uuid.UUID] = None, limit: int = 20,
                                   membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """Hybride Belegsuche: Volltext (deutsche Wortstaemme) und semantische
    Naehe (lokales Embedding-Modell), zusammengefuehrt per Reciprocal Rank
    Fusion. Beide Wege filtern VOR dem Ranking auf Mandant, freigegebene
    (indexierte) und nicht ersetzte Versionen -- zusaetzlich greift RLS.
    Semantische Naehe liefert Kontext, gibt aber nie eine Preisposition fuer
    den Vergleich frei."""
    from services.mdc_embeddings import embed_texts, to_pgvector, MODEL_NAME
    if not q.strip():
        raise HTTPException(400, "Suchbegriff fehlt.")
    limit = max(1, min(limit, 50))
    params = {"q": q, "tenant_id": str(membership.tenant_id),
              "category_id": str(category_id) if category_id else None, "k": SEARCH_CANDIDATES}

    fts_rows = (await db.execute(sql_text(f"""
        SELECT c.id, ts_rank(to_tsvector('german', c.text), websearch_to_tsquery('german', :q)) AS rank
        {_SEARCH_SCOPE}
          AND to_tsvector('german', c.text) @@ websearch_to_tsquery('german', :q)
        ORDER BY rank DESC, c.id
        LIMIT :k
    """), params)).all()

    semantic_ok, sem_rows = True, []
    try:
        qvec = (await asyncio.to_thread(embed_texts, [q.strip()]))[0]
        async with db.begin_nested():  # Fehler hier darf die Volltext-Transaktion nicht abbrechen
            # Iterativer HNSW-Scan: sonst kann der Mandantenfilter nach dem Index
            # die Trefferliste bei vielen Mandanten unvollstaendig machen.
            await db.execute(sql_text("SET LOCAL hnsw.iterative_scan = 'relaxed_order'"))
            sem_rows = (await db.execute(sql_text(f"""
                SELECT c.id, 1 - (c.embedding <=> CAST(:qv AS vector)) AS similarity
                {_SEARCH_SCOPE}
                  AND c.embedding IS NOT NULL AND c.embedding_model = :model
                ORDER BY c.embedding <=> CAST(:qv AS vector), c.id
                LIMIT :k
            """), {**params, "qv": to_pgvector(qvec), "model": MODEL_NAME})).all()
        sem_rows = sorted((r for r in sem_rows if float(r.similarity) >= SEMANTIC_MIN_SIMILARITY),
                          key=lambda r: (-float(r.similarity), str(r.id)))
    except Exception:
        logger.exception("Semantische Suche nicht verfuegbar -- nur Volltext.")
        semantic_ok = False

    fused: dict = {}
    for rank, r in enumerate(fts_rows, start=1):
        e = fused.setdefault(r.id, {"score": 0.0, "fts_rank": None, "similarity": None})
        e["score"] += 1 / (RRF_K + rank)
        e["fts_rank"] = round(float(r.rank), 4)
    for rank, r in enumerate(sem_rows, start=1):
        e = fused.setdefault(r.id, {"score": 0.0, "fts_rank": None, "similarity": None})
        e["score"] += 1 / (RRF_K + rank)
        e["similarity"] = round(float(r.similarity), 4)
    top = sorted(fused.items(), key=lambda kv: (-kv[1]["score"], str(kv[0])))[:limit]

    hits = []
    if top:
        details = {r.id: r for r in (await db.execute(sql_text(f"""
            SELECT c.id, c.anchor, c.text, v.id AS version_id, v.version_number, d.id AS document_id, d.title,
                   ts_headline('german', c.text, websearch_to_tsquery('german', :q),
                               'StartSel=<<, StopSel=>>, MaxFragments=2, MaxWords=30, MinWords=8') AS headline
            {_SEARCH_SCOPE}
              AND c.id = ANY(CAST(:ids AS uuid[]))
        """), {**params, "ids": [str(cid) for cid, _ in top]})).all()}
        for cid, e in top:
            r = details.get(cid)
            if not r:
                continue
            match = "beides" if e["fts_rank"] is not None and e["similarity"] is not None else (
                "volltext" if e["fts_rank"] is not None else "semantisch")
            hits.append({
                "chunk_id": str(cid), "anchor": r.anchor, "document_id": str(r.document_id), "document_title": r.title,
                "document_version_id": str(r.version_id), "version_number": r.version_number,
                "match": match, "relevance": round(e["score"], 5), "fts_rank": e["fts_rank"],
                "semantic_similarity": e["similarity"],
                "snippet": r.headline if e["fts_rank"] is not None else r.text[:300],
            })
    return {"query": q, "semantic_available": semantic_ok, "embedding_model": MODEL_NAME if semantic_ok else None,
            "fusion": f"Reciprocal Rank Fusion (k={RRF_K})", "hits": hits}


class ReviewRequest(BaseModel):
    reason: str


@router.post("/line-items/{line_item_id}/request-review")
async def request_data_review(line_item_id: uuid.UUID, payload: ReviewRequest, user=Depends(get_current_user),
                              membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """Unklarer Wert -> Pruefaufgabe. Eine freigegebene Position verlaesst
    damit sofort den Vergleichsbestand und die Belegsuche, bis ein Mensch
    sie erneut freigibt. Bestehende Analyse-Snapshots bleiben unveraendert."""
    from routers.mdc import _refresh_version_status
    if not payload.reason.strip():
        raise HTTPException(400, "Bitte den Grund der Pruefanfrage angeben.")
    item, version, doc, supplier = await _load_item(db, line_item_id, membership.tenant_id)
    if item.review_status in (MDCReviewStatus.superseded, MDCReviewStatus.rejected):
        raise HTTPException(400, f"Position im Status '{_ev(item.review_status)}' kann nicht erneut geprueft werden.")
    previous = _ev(item.review_status)
    item.review_status = MDCReviewStatus.needs_review
    item.review_note = f"Pruefanfrage von {user.id} (vorher {previous}): {payload.reason.strip()[:500]}"
    item.reviewed_by = None
    item.reviewed_at = None
    issues = [i for i in (item.open_issues_json or []) if i.get("code") != "review_requested"]
    issues.append({"code": "review_requested", "message": f"Pruefanfrage: {payload.reason.strip()[:300]}", "blocking": True})
    item.open_issues_json = issues
    await _refresh_version_status(db, version)
    await db.commit()
    return {"line_item_id": str(item.id), "review_status": "needs_review", "previous_status": previous}
