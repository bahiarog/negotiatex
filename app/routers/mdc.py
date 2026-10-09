"""
Master Data Center -- Etappe 1 (Datenkern). Siehe models_mdc.py fuer die
bewusste Abgrenzung zu Etappe 2/3 (Preispositionen, Review-Queue, RAG,
Analytik kommen spaeter).

Pilot-Kategorie (mit dem Gruender/CTO festgelegt): Video-Produktion /
Agentur-Ratecards. Datenbasis vorerst ausschliesslich Dummy-/Testdokumente,
keine echten Mandantendaten (siehe Bericht).

Upload-Pipeline deckt Schritte 1-3 der Anleitung ab (autorisieren, Hash/
Version/Duplikat erkennen, Text+Tabellen lesen). Schritte 4-6 (strukturierte
Preis-Extraktion, Normalisierung, Validierung/Review-Queue) sind Etappe 2
und bewusst noch nicht gebaut -- Versionen bleiben nach dem Parsen im
Status 'parsed', nicht 'approved'.
"""
import asyncio
import hashlib
import logging
import os
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select, desc, delete as sa_delete, text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from deps import get_current_user, get_current_membership
from models_mdc import (
    MDCCategory, MDCSupplier, MDCDocument, MDCDocumentVersion, MDCImportStatus, MDCDocumentType,
    MDCLineItem, MDCReviewStatus, MDCPriceStatus, MDCTaxBasis, MDCRetrievalChunk,
)
from services.pdf_parser import extract_text
from services.mdc_search import build_chunks, CHUNKING_VERSION
from services.mdc_embeddings import embed_texts, to_pgvector, MODEL_NAME as EMBEDDING_MODEL, DIM as EMBEDDING_DIM
from services.mdc_extractor import extract_line_items, normalize_and_check, ANCILLARY_KEYS, _to_decimal

logger = logging.getLogger(__name__)
router = APIRouter()

UPLOAD_DIR = Path(os.getenv("MDC_UPLOAD_DIR", "/app/uploads/mdc"))
MAX_FILE_SIZE = 25 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".xlsx", ".xls", ".csv", ".doc", ".docx"}


# ---------------------------------------------------------------------------
# Kategorien
# ---------------------------------------------------------------------------

class CategoryCreate(BaseModel):
    name: str
    must_criteria_json: Optional[dict] = None


def _category_to_dict(c: MDCCategory) -> dict:
    return {
        "id": str(c.id), "name": c.name, "version": c.version,
        "must_criteria_json": c.must_criteria_json, "created_at": c.created_at,
    }


@router.post("/categories")
async def create_category(payload: CategoryCreate, user=Depends(get_current_user),
                           membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    if not payload.name.strip():
        raise HTTPException(400, "Name ist ein Pflichtfeld.")
    cat = MDCCategory(tenant_id=membership.tenant_id, name=payload.name.strip(),
                       must_criteria_json=payload.must_criteria_json or {}, created_by=str(user.id))
    db.add(cat)
    await db.commit()
    await db.refresh(cat)
    return _category_to_dict(cat)


@router.get("/categories")
async def list_categories(membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(MDCCategory).where(MDCCategory.tenant_id == membership.tenant_id).order_by(MDCCategory.created_at))
    return [_category_to_dict(c) for c in r.scalars().all()]


# ---------------------------------------------------------------------------
# Lieferanten (Master-Data-Identitaet, getrennt von Sourcing-Kandidaten)
# ---------------------------------------------------------------------------

async def _get_or_create_mdc_supplier(db: AsyncSession, tenant_id, name: Optional[str]) -> Optional[uuid.UUID]:
    if not name or not name.strip():
        return None
    name = name.strip()
    r = await db.execute(select(MDCSupplier).where(MDCSupplier.tenant_id == tenant_id, MDCSupplier.name == name))
    s = r.scalar_one_or_none()
    if s:
        return s.id
    s = MDCSupplier(tenant_id=tenant_id, name=name)
    db.add(s)
    await db.flush()
    return s.id


@router.get("/suppliers")
async def list_mdc_suppliers(membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(MDCSupplier).where(MDCSupplier.tenant_id == membership.tenant_id).order_by(MDCSupplier.name))
    return [{"id": str(s.id), "name": s.name, "domain": s.domain, "confirmed": s.confirmed} for s in r.scalars().all()]


# ---------------------------------------------------------------------------
# Dokumente / Versionen (Upload-Pipeline Schritte 1-3)
# ---------------------------------------------------------------------------

def _version_to_dict(v: MDCDocumentVersion) -> dict:
    return {
        "id": str(v.id), "document_id": str(v.document_id), "version_number": v.version_number,
        "file_name": v.file_name, "file_hash": v.file_hash, "file_size_bytes": v.file_size_bytes,
        "source": v.source, "rights_note": v.rights_note,
        "import_status": v.import_status.value if hasattr(v.import_status, "value") else v.import_status,
        "import_error": v.import_error,
        "superseded_by_version_id": str(v.superseded_by_version_id) if v.superseded_by_version_id else None,
        "created_at": v.created_at,
    }


def _document_to_dict(d: MDCDocument, versions: list[MDCDocumentVersion]) -> dict:
    return {
        "id": str(d.id), "category_id": str(d.category_id) if d.category_id else None,
        "supplier_id": str(d.supplier_id) if d.supplier_id else None,
        "document_type": d.document_type.value if hasattr(d.document_type, "value") else d.document_type,
        "title": d.title, "created_at": d.created_at,
        "versions": [_version_to_dict(v) for v in versions],
    }


@router.post("/documents/upload")
async def upload_document(
    file: UploadFile = File(...),
    category_id: Optional[uuid.UUID] = Form(None),
    supplier_name: Optional[str] = Form(None),
    document_type: str = Form("other"),
    title: Optional[str] = Form(None),
    source: Optional[str] = Form(None),
    rights_note: Optional[str] = Form(None),
    revision_of_document_id: Optional[uuid.UUID] = Form(None),
    user=Depends(get_current_user), membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db),
):
    """Schritte 1-3: autorisieren (ueber get_current_membership, Mandant aus
    Session), Hash/Quelle/Version erfassen und exakte Duplikate mandanten-
    bezogen erkennen, Text/Tabellen lesen. Ein exaktes Duplikat (gleicher
    Hash, gleicher Mandant) erzeugt KEINE neue Version -- es wird die
    bestehende zurueckgegeben (Schritt 8: 'ein Retry darf keine zusaetzlichen
    Preisbeobachtungen erzeugen')."""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Dateityp nicht unterstuetzt. Erlaubt: {sorted(ALLOWED_EXTENSIONS)}.")
    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(400, "Datei zu gross (max. 25 MB).")
    if not content:
        raise HTTPException(400, "Datei ist leer.")

    try:
        doc_type = MDCDocumentType(document_type)
    except ValueError:
        raise HTTPException(400, f"Ungueltiger document_type. Erlaubt: {[t.value for t in MDCDocumentType]}.")

    file_hash = hashlib.sha256(content).hexdigest()
    tenant_id = membership.tenant_id

    existing = await db.execute(select(MDCDocumentVersion).where(
        MDCDocumentVersion.tenant_id == tenant_id, MDCDocumentVersion.file_hash == file_hash,
    ))
    dup = existing.scalar_one_or_none()
    if dup:
        doc = (await db.execute(select(MDCDocument).where(MDCDocument.id == dup.document_id))).scalar_one()
        versions = (await db.execute(select(MDCDocumentVersion).where(MDCDocumentVersion.document_id == doc.id).order_by(MDCDocumentVersion.version_number))).scalars().all()
        result = _document_to_dict(doc, versions)
        result["duplicate"] = True
        return result

    cat_uuid = None
    if category_id:
        cat_uuid = category_id
        cat = (await db.execute(select(MDCCategory).where(MDCCategory.id == category_id, MDCCategory.tenant_id == tenant_id))).scalar_one_or_none()
        if not cat:
            raise HTTPException(404, "Kategorie nicht gefunden.")

    supplier_id = await _get_or_create_mdc_supplier(db, tenant_id, supplier_name)

    if revision_of_document_id:
        doc = (await db.execute(select(MDCDocument).where(MDCDocument.id == revision_of_document_id, MDCDocument.tenant_id == tenant_id))).scalar_one_or_none()
        if not doc:
            raise HTTPException(404, "Zu revisionierendes Dokument nicht gefunden.")
        prior_versions = (await db.execute(select(MDCDocumentVersion).where(MDCDocumentVersion.document_id == doc.id).order_by(desc(MDCDocumentVersion.version_number)))).scalars().all()
        next_version_number = (prior_versions[0].version_number + 1) if prior_versions else 1
    else:
        doc = MDCDocument(tenant_id=tenant_id, category_id=cat_uuid, supplier_id=supplier_id,
                           document_type=doc_type, title=title or file.filename, created_by=str(user.id))
        db.add(doc)
        await db.flush()
        prior_versions = []
        next_version_number = 1

    tenant_dir = UPLOAD_DIR / str(tenant_id)
    tenant_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{file_hash}{suffix}"
    stored_path = tenant_dir / stored_name
    if not stored_path.exists():
        stored_path.write_bytes(content)

    version = MDCDocumentVersion(
        tenant_id=tenant_id, document_id=doc.id, version_number=next_version_number,
        file_name=file.filename or stored_name, file_path=str(stored_path), file_hash=file_hash,
        file_size_bytes=len(content), source=source, rights_note=rights_note,
        import_status=MDCImportStatus.uploaded, created_by=str(user.id),
    )
    db.add(version)
    await db.flush()

    try:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        try:
            text = await extract_text(tmp_path, file.filename or stored_name)
        finally:
            os.unlink(tmp_path)
        if text and not text.startswith("[Error") and not text.startswith("[Unsupported"):
            version.extracted_text = text
            version.import_status = MDCImportStatus.parsed
        else:
            version.import_error = text or "Kein Text extrahiert."
            version.import_status = MDCImportStatus.needs_review
    except Exception as e:
        logger.exception(f"MDC-Import: Text-Extraktion fehlgeschlagen fuer Version {version.id}")
        version.import_error = str(e)
        version.import_status = MDCImportStatus.needs_review

    if prior_versions:
        prior_versions[0].import_status = MDCImportStatus.superseded
        prior_versions[0].superseded_by_version_id = version.id
        await db.execute(sa_delete(MDCRetrievalChunk).where(MDCRetrievalChunk.document_version_id == prior_versions[0].id))

    await db.commit()
    all_versions = (await db.execute(select(MDCDocumentVersion).where(MDCDocumentVersion.document_id == doc.id).order_by(MDCDocumentVersion.version_number))).scalars().all()
    result = _document_to_dict(doc, all_versions)
    result["duplicate"] = False
    return result


@router.get("/documents")
async def list_documents(membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(MDCDocument).where(MDCDocument.tenant_id == membership.tenant_id).order_by(desc(MDCDocument.created_at)))
    docs = r.scalars().all()
    results = []
    for d in docs:
        versions = (await db.execute(select(MDCDocumentVersion).where(MDCDocumentVersion.document_id == d.id).order_by(MDCDocumentVersion.version_number))).scalars().all()
        results.append(_document_to_dict(d, versions))
    return results


async def _get_document_or_404(document_id: str, tenant_id, db: AsyncSession) -> MDCDocument:
    r = await db.execute(select(MDCDocument).where(MDCDocument.id == document_id, MDCDocument.tenant_id == tenant_id))
    doc = r.scalar_one_or_none()
    if not doc:
        raise HTTPException(404, "Dokument nicht gefunden.")
    return doc


@router.get("/documents/{document_id}")
async def get_document(document_id: uuid.UUID, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    doc = await _get_document_or_404(document_id, membership.tenant_id, db)
    versions = (await db.execute(select(MDCDocumentVersion).where(MDCDocumentVersion.document_id == doc.id).order_by(MDCDocumentVersion.version_number))).scalars().all()
    return _document_to_dict(doc, versions)


@router.get("/documents/{document_id}/versions/{version_id}/text")
async def get_version_text(document_id: uuid.UUID, version_id: uuid.UUID, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    doc = await _get_document_or_404(document_id, membership.tenant_id, db)
    r = await db.execute(select(MDCDocumentVersion).where(MDCDocumentVersion.id == version_id, MDCDocumentVersion.document_id == doc.id))
    v = r.scalar_one_or_none()
    if not v:
        raise HTTPException(404, "Version nicht gefunden.")
    return {"extracted_text": v.extracted_text, "import_status": v.import_status.value, "import_error": v.import_error}


@router.get("/documents/{document_id}/versions/{version_id}/download")
async def download_version(document_id: uuid.UUID, version_id: uuid.UUID, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """Sichere, authentifizierte Download-Route statt frei waehlbarer
    Dateipfade -- das Original wird nie ueber einen direkt erratbaren Pfad
    ausgeliefert, sondern nur ueber Version-ID nach Mandantenpruefung."""
    doc = await _get_document_or_404(document_id, membership.tenant_id, db)
    r = await db.execute(select(MDCDocumentVersion).where(MDCDocumentVersion.id == version_id, MDCDocumentVersion.document_id == doc.id))
    v = r.scalar_one_or_none()
    if not v or not os.path.exists(v.file_path):
        raise HTTPException(404, "Originaldatei nicht gefunden.")
    return FileResponse(v.file_path, filename=v.file_name)


# ---------------------------------------------------------------------------
# Etappe 2 -- Preispositionen: Extraktion (Vorschlag), Pruefung, Freigabe
# ---------------------------------------------------------------------------
# Das LLM schlaegt nur vor (services/mdc_extractor.extract_line_items, ohne
# tools). Ob eine Position in Vergleiche einfliessen darf, entscheidet
# ausschliesslich ein Mensch ueber /approve -- und nur, wenn der
# deterministische Check keine blockierenden Punkte mehr meldet.

DOC_TYPE_TO_PRICE_STATUS = {
    "ratecard": MDCPriceStatus.list_price, "price_list": MDCPriceStatus.list_price,
    "offer": MDCPriceStatus.quoted, "contract": MDCPriceStatus.contracted,
    "invoice": MDCPriceStatus.invoiced, "other": MDCPriceStatus.quoted,
}

LINE_ITEM_TEXT_LIMITS = {
    "role_or_item": 255, "seniority": 100, "region": 100, "original_unit": 50,
    "original_currency": 10, "original_amount_raw": 100, "payment_terms": 255,
}


def _s(v, field: str):
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    limit = LINE_ITEM_TEXT_LIMITS.get(field)
    return s[:limit] if limit else s


def _dec(v):
    return _to_decimal(v)


def _parse_date(v) -> Optional[date]:
    if not v:
        return None
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _clean_ancillary(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for k in ANCILLARY_KEYS:
        entry = raw.get(k) if isinstance(raw.get(k), dict) else {}
        status = entry.get("status") if entry.get("status") in ("inclusive", "exclusive", "unknown") else "unknown"
        amount = _dec(entry.get("amount"))
        out[k] = {"status": status, "amount": str(amount) if amount is not None else None}
    return out


def _coerce_tax_basis(v) -> MDCTaxBasis:
    try:
        return MDCTaxBasis(v)
    except ValueError:
        return MDCTaxBasis.unknown


def _enum_val(v):
    return v.value if hasattr(v, "value") else v


def _fields_of(item: MDCLineItem) -> dict:
    return {
        "role_or_item": item.role_or_item, "original_amount": item.original_amount,
        "original_amount_raw": item.original_amount_raw, "amount_confirmed": item.amount_confirmed,
        "original_currency": item.original_currency, "original_unit": item.original_unit,
        "tax_basis": _enum_val(item.tax_basis), "tax_rate_pct": item.tax_rate_pct,
        "billable_hours_per_day": item.billable_hours_per_day,
        "ancillary_costs_json": item.ancillary_costs_json, "source_evidence": item.source_evidence,
        "offer_date": item.offer_date, "valid_from": item.valid_from, "valid_to": item.valid_to,
    }


def _apply_check(item: MDCLineItem) -> bool:
    """Rechnet Normalisierung neu und setzt den Pruefstatus fuer noch nicht
    entschiedene Positionen. Gibt zurueck, ob blockierende Punkte bestehen."""
    result = normalize_and_check(_fields_of(item))
    item.original_currency = result["original_currency"]
    item.normalized_amount_net = result["normalized_amount_net"]
    item.canonical_unit = result["canonical_unit"]
    item.normalized_amount_per_canonical_unit = result["normalized_amount_per_canonical_unit"]
    item.normalization_version = result["normalization_version"]
    item.open_issues_json = result["open_issues_json"]
    if item.review_status in (MDCReviewStatus.extracted, MDCReviewStatus.needs_review):
        item.review_status = MDCReviewStatus.needs_review if result["has_blocking_issues"] else MDCReviewStatus.extracted
    return result["has_blocking_issues"]


def _match_key(item: MDCLineItem) -> tuple:
    return tuple((x or "").strip().lower() for x in (item.role_or_item, item.seniority, item.region, item.canonical_unit or item.original_unit))


def _money(v):
    return str(v) if v is not None else None


def _line_item_to_dict(i: MDCLineItem) -> dict:
    return {
        "id": str(i.id), "document_version_id": str(i.document_version_id),
        "category_id": str(i.category_id) if i.category_id else None,
        "supplier_id": str(i.supplier_id) if i.supplier_id else None,
        "role_or_item": i.role_or_item, "scope_text": i.scope_text, "seniority": i.seniority, "region": i.region,
        "original_amount": _money(i.original_amount), "original_amount_raw": i.original_amount_raw,
        "amount_confirmed": i.amount_confirmed, "original_currency": i.original_currency,
        "tax_basis": _enum_val(i.tax_basis), "tax_rate_pct": _money(i.tax_rate_pct),
        "normalized_amount_net": _money(i.normalized_amount_net), "normalization_version": i.normalization_version,
        "original_unit": i.original_unit, "canonical_unit": i.canonical_unit,
        "quantity": _money(i.quantity), "min_quantity": _money(i.min_quantity),
        "billable_hours_per_day": _money(i.billable_hours_per_day),
        "normalized_amount_per_canonical_unit": _money(i.normalized_amount_per_canonical_unit),
        "ancillary_costs_json": i.ancillary_costs_json, "payment_terms": i.payment_terms,
        "price_status": _enum_val(i.price_status), "review_status": _enum_val(i.review_status),
        "open_issues_json": i.open_issues_json or [],
        "offer_date": i.offer_date, "valid_from": i.valid_from, "valid_to": i.valid_to,
        "source_evidence": i.source_evidence, "reviewed_by": i.reviewed_by, "reviewed_at": i.reviewed_at,
        "review_note": i.review_note,
        "superseded_by_line_item_id": str(i.superseded_by_line_item_id) if i.superseded_by_line_item_id else None,
        "created_at": i.created_at, "updated_at": i.updated_at,
    }


async def _refresh_version_status(db: AsyncSession, version: MDCDocumentVersion) -> None:
    """Leitet den Versionsstatus aus den Positionen ab. Nur eine vollstaendig
    entschiedene Version wird in die Belegsuche aufgenommen (APPROVED ->
    INDEXED); wird eine Position wieder geoeffnet, verschwinden ihre
    Suchabschnitte sofort wieder."""
    if version.import_status == MDCImportStatus.superseded:
        await db.execute(sa_delete(MDCRetrievalChunk).where(MDCRetrievalChunk.document_version_id == version.id))
        return
    items = (await db.execute(select(MDCLineItem).where(MDCLineItem.document_version_id == version.id))).scalars().all()
    active = [i for i in items if i.review_status != MDCReviewStatus.superseded]
    if not active:
        return
    if any(i.review_status == MDCReviewStatus.needs_review for i in active):
        new_status = MDCImportStatus.needs_review
    elif all(i.review_status in (MDCReviewStatus.approved, MDCReviewStatus.rejected) for i in active):
        new_status = MDCImportStatus.approved
    else:
        new_status = MDCImportStatus.extracted

    if new_status != MDCImportStatus.approved:
        await db.execute(sa_delete(MDCRetrievalChunk).where(MDCRetrievalChunk.document_version_id == version.id))
        version.import_status = new_status
        return
    if version.import_status == MDCImportStatus.indexed:
        return
    await db.execute(sa_delete(MDCRetrievalChunk).where(MDCRetrievalChunk.document_version_id == version.id))
    chunks = []
    for idx, (anchor, chunk_text) in enumerate(build_chunks(version.extracted_text or "")):
        chunk = MDCRetrievalChunk(tenant_id=version.tenant_id, document_version_id=version.id, chunk_index=idx,
                                  anchor=anchor or None, text=chunk_text, chunking_version=CHUNKING_VERSION)
        db.add(chunk)
        chunks.append(chunk)
    await db.flush()
    await embed_chunks(db, chunks)
    version.import_status = MDCImportStatus.indexed


async def embed_chunks(db: AsyncSession, chunks: list) -> int:
    """Berechnet die Vektoren lokal und schreibt sie per SQL (die Spalte ist
    nicht im ORM-Modell gemappt). Scheitert das Modell, bleiben die
    Abschnitte per Volltext auffindbar -- die Freigabe selbst wird dadurch
    nicht blockiert."""
    if not chunks:
        return 0
    try:
        vectors = await asyncio.to_thread(embed_texts, [c.text for c in chunks])
    except Exception:
        logger.exception("Embedding fehlgeschlagen -- Abschnitte bleiben nur per Volltext durchsuchbar.")
        return 0
    for chunk, vec in zip(chunks, vectors):
        await db.execute(
            sql_text("UPDATE mdc_retrieval_chunks SET embedding = CAST(:v AS vector), embedding_model = :m, "
                     "embedding_dim = :d, index_status = 'fts+vector' WHERE id = :id"),
            {"v": to_pgvector(vec), "m": EMBEDDING_MODEL, "d": EMBEDDING_DIM, "id": chunk.id},
        )
    return len(chunks)


async def _get_version_or_404(doc: MDCDocument, version_id: str, db: AsyncSession) -> MDCDocumentVersion:
    r = await db.execute(select(MDCDocumentVersion).where(MDCDocumentVersion.id == version_id, MDCDocumentVersion.document_id == doc.id))
    v = r.scalar_one_or_none()
    if not v:
        raise HTTPException(404, "Version nicht gefunden.")
    return v


@router.post("/documents/{document_id}/versions/{version_id}/extract-lines")
async def extract_lines(
    document_id: uuid.UUID, version_id: uuid.UUID, replace: bool = False,
    user=Depends(get_current_user), membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db),
):
    """Schritte 4-6 der Import-Pipeline. Idempotent: eine bereits
    extrahierte Version wird nicht erneut extrahiert (ein Retry erzeugt
    keine zusaetzlichen Preisbeobachtungen). replace=true verwirft nur
    ungepruefte Vorschlaege -- sobald eine Position entschieden wurde, ist
    erneutes Extrahieren gesperrt."""
    doc = await _get_document_or_404(document_id, membership.tenant_id, db)
    v = await _get_version_or_404(doc, version_id, db)
    if v.import_status == MDCImportStatus.superseded:
        raise HTTPException(400, "Diese Version ist superseded -- bitte die aktuelle Version extrahieren.")
    if not v.extracted_text:
        raise HTTPException(400, "Kein lesbarer Dokumenttext vorhanden (Import-Status pruefen).")

    existing = (await db.execute(select(MDCLineItem).where(MDCLineItem.document_version_id == v.id))).scalars().all()
    if existing:
        if not replace:
            raise HTTPException(409, "Fuer diese Version wurden bereits Positionen extrahiert. Erneut nur mit replace=true.")
        if any(i.review_status in (MDCReviewStatus.approved, MDCReviewStatus.rejected) for i in existing):
            raise HTTPException(409, "Diese Version enthaelt bereits gepruefte Positionen -- erneute Extraktion wuerde Pruefergebnisse verwerfen.")
        for i in existing:
            await db.delete(i)
        await db.flush()

    must = None
    if doc.category_id:
        cat = (await db.execute(select(MDCCategory).where(MDCCategory.id == doc.category_id))).scalar_one_or_none()
        must = cat.must_criteria_json if cat else None

    doc_type = _enum_val(doc.document_type)
    try:
        raw_items = await asyncio.to_thread(extract_line_items, v.extracted_text, doc_type, must)
    except Exception as e:
        logger.exception(f"MDC-Extraktion fehlgeschlagen fuer Version {v.id}")
        v.import_error = f"Extraktion fehlgeschlagen: {e}"
        v.import_status = MDCImportStatus.needs_review
        await db.commit()
        raise HTTPException(502, "Extraktion fehlgeschlagen -- Version zur manuellen Pruefung markiert.")

    price_status = DOC_TYPE_TO_PRICE_STATUS.get(doc_type, MDCPriceStatus.quoted)
    new_items: list[MDCLineItem] = []
    for r in raw_items:
        item = MDCLineItem(
            tenant_id=membership.tenant_id, document_version_id=v.id, category_id=doc.category_id,
            supplier_id=doc.supplier_id,
            role_or_item=_s(r.get("role_or_item"), "role_or_item"), seniority=_s(r.get("seniority"), "seniority"),
            region=_s(r.get("region"), "region"), scope_text=_s(r.get("scope_text"), "scope_text"),
            original_amount=_dec(r.get("amount")), original_amount_raw=_s(r.get("amount_raw"), "original_amount_raw"),
            original_currency=_s(r.get("currency"), "original_currency"),
            tax_basis=_coerce_tax_basis(r.get("tax_basis")), tax_rate_pct=_dec(r.get("tax_rate_pct")),
            original_unit=_s(r.get("unit"), "original_unit"),
            billable_hours_per_day=_dec(r.get("billable_hours_per_day")),
            quantity=_dec(r.get("quantity")), min_quantity=_dec(r.get("min_quantity")),
            ancillary_costs_json=_clean_ancillary(r.get("ancillary_costs")),
            payment_terms=_s(r.get("payment_terms"), "payment_terms"),
            offer_date=_parse_date(r.get("offer_date")), valid_from=_parse_date(r.get("valid_from")),
            valid_to=_parse_date(r.get("valid_to")), source_evidence=_s(r.get("source_evidence"), "source_evidence"),
            price_status=price_status, review_status=MDCReviewStatus.extracted, created_by="system:extraction",
        )
        _apply_check(item)
        db.add(item)
        new_items.append(item)
    await db.flush()

    # Positionen frueherer Versionen desselben Dokuments werden superseded,
    # nicht geloescht -- eine Revision ist kein zusaetzlicher Marktbeleg.
    prior_versions = (await db.execute(select(MDCDocumentVersion).where(
        MDCDocumentVersion.document_id == doc.id, MDCDocumentVersion.version_number < v.version_number,
    ))).scalars().all()
    if prior_versions:
        by_key = {_match_key(i): i for i in new_items}
        prior_items = (await db.execute(select(MDCLineItem).where(
            MDCLineItem.document_version_id.in_([pv.id for pv in prior_versions]),
            MDCLineItem.review_status.in_([MDCReviewStatus.extracted, MDCReviewStatus.needs_review, MDCReviewStatus.approved]),
        ))).scalars().all()
        for old in prior_items:
            old.review_status = MDCReviewStatus.superseded
            match = by_key.get(_match_key(old))
            if match:
                old.superseded_by_line_item_id = match.id

    v.import_error = None
    await _refresh_version_status(db, v)
    await db.commit()
    items = (await db.execute(select(MDCLineItem).where(MDCLineItem.document_version_id == v.id).order_by(MDCLineItem.created_at))).scalars().all()
    return {"version_import_status": _enum_val(v.import_status), "line_items": [_line_item_to_dict(i) for i in items]}


@router.get("/documents/{document_id}/versions/{version_id}/line-items")
async def list_version_line_items(document_id: uuid.UUID, version_id: uuid.UUID, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    doc = await _get_document_or_404(document_id, membership.tenant_id, db)
    v = await _get_version_or_404(doc, version_id, db)
    items = (await db.execute(select(MDCLineItem).where(MDCLineItem.document_version_id == v.id).order_by(MDCLineItem.created_at))).scalars().all()
    return [_line_item_to_dict(i) for i in items]


@router.get("/line-items")
async def list_line_items(review_status: Optional[str] = None, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """Review-Queue: standardmaessig alle noch nicht entschiedenen Positionen."""
    q = select(MDCLineItem, MDCDocumentVersion, MDCDocument).join(
        MDCDocumentVersion, MDCLineItem.document_version_id == MDCDocumentVersion.id,
    ).join(MDCDocument, MDCDocumentVersion.document_id == MDCDocument.id).where(MDCLineItem.tenant_id == membership.tenant_id)
    if review_status:
        try:
            q = q.where(MDCLineItem.review_status == MDCReviewStatus(review_status))
        except ValueError:
            raise HTTPException(400, f"Ungueltiger review_status. Erlaubt: {[s.value for s in MDCReviewStatus]}.")
    else:
        q = q.where(MDCLineItem.review_status.in_([MDCReviewStatus.extracted, MDCReviewStatus.needs_review]))
    rows = (await db.execute(q.order_by(desc(MDCLineItem.created_at)))).all()
    out = []
    for item, version, doc in rows:
        d = _line_item_to_dict(item)
        d["document_id"] = str(doc.id)
        d["document_title"] = doc.title
        d["version_number"] = version.version_number
        out.append(d)
    return out


async def _get_line_item_or_404(item_id: str, tenant_id, db: AsyncSession) -> MDCLineItem:
    r = await db.execute(select(MDCLineItem).where(MDCLineItem.id == item_id, MDCLineItem.tenant_id == tenant_id))
    item = r.scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Position nicht gefunden.")
    return item


@router.get("/line-items/{item_id}")
async def get_line_item(item_id: uuid.UUID, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    item = await _get_line_item_or_404(item_id, membership.tenant_id, db)
    version = (await db.execute(select(MDCDocumentVersion).where(MDCDocumentVersion.id == item.document_version_id))).scalar_one()
    d = _line_item_to_dict(item)
    d["document_id"] = str(version.document_id)
    d["version_number"] = version.version_number
    d["source_text"] = version.extracted_text
    return d


class LineItemUpdate(BaseModel):
    role_or_item: Optional[str] = None
    scope_text: Optional[str] = None
    seniority: Optional[str] = None
    region: Optional[str] = None
    original_amount: Optional[str] = None
    original_currency: Optional[str] = None
    tax_basis: Optional[str] = None
    tax_rate_pct: Optional[str] = None
    original_unit: Optional[str] = None
    billable_hours_per_day: Optional[str] = None
    quantity: Optional[str] = None
    min_quantity: Optional[str] = None
    ancillary_costs_json: Optional[dict] = None
    payment_terms: Optional[str] = None
    price_status: Optional[str] = None
    offer_date: Optional[str] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    source_evidence: Optional[str] = None
    confirm_amount: Optional[bool] = None


@router.put("/line-items/{item_id}")
async def update_line_item(item_id: uuid.UUID, payload: LineItemUpdate, user=Depends(get_current_user),
                            membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """Menschliche Korrektur. Eine bereits entschiedene Position, die
    geaendert wird, verliert ihre Freigabe -- eine Freigabe gilt nur fuer
    genau die gepruefte Fassung."""
    item = await _get_line_item_or_404(item_id, membership.tenant_id, db)
    if item.review_status == MDCReviewStatus.superseded:
        raise HTTPException(400, "Position ist superseded und kann nicht mehr geaendert werden.")

    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(400, "Keine Aenderung uebergeben.")

    text_fields = ("role_or_item", "scope_text", "seniority", "region", "original_currency", "original_unit", "payment_terms", "source_evidence")
    decimal_fields = ("original_amount", "tax_rate_pct", "billable_hours_per_day", "quantity", "min_quantity")
    date_fields = ("offer_date", "valid_from", "valid_to")

    for f in text_fields:
        if f in data:
            setattr(item, f, _s(data[f], f))
    for f in decimal_fields:
        if f in data:
            val = _dec(data[f])
            if data[f] not in (None, "") and val is None:
                raise HTTPException(400, f"{f}: kein gueltiger Zahlenwert (Punkt als Dezimaltrenner).")
            setattr(item, f, val)
    for f in date_fields:
        if f in data:
            val = _parse_date(data[f])
            if data[f] and val is None:
                raise HTTPException(400, f"{f}: Datum im Format YYYY-MM-DD erwartet.")
            setattr(item, f, val)
    if "tax_basis" in data:
        if data["tax_basis"] not in ("net", "gross", "unknown"):
            raise HTTPException(400, "tax_basis muss net, gross oder unknown sein.")
        item.tax_basis = MDCTaxBasis(data["tax_basis"])
    if "price_status" in data:
        try:
            item.price_status = MDCPriceStatus(data["price_status"])
        except ValueError:
            raise HTTPException(400, f"Ungueltiger price_status. Erlaubt: {[s.value for s in MDCPriceStatus]}.")
    if "ancillary_costs_json" in data:
        item.ancillary_costs_json = _clean_ancillary(data["ancillary_costs_json"])
    if "original_amount" in data:
        item.amount_confirmed = False
    if data.get("confirm_amount"):
        item.amount_confirmed = True

    if item.review_status in (MDCReviewStatus.approved, MDCReviewStatus.rejected):
        item.review_status = MDCReviewStatus.extracted
        item.review_note = f"Nach Entscheidung von {item.reviewed_by} geaendert durch {user.id} -- erneute Pruefung noetig."
        item.reviewed_by = None
        item.reviewed_at = None

    _apply_check(item)
    version = (await db.execute(select(MDCDocumentVersion).where(MDCDocumentVersion.id == item.document_version_id))).scalar_one()
    await _refresh_version_status(db, version)
    await db.commit()
    await db.refresh(item)
    return _line_item_to_dict(item)


class ReviewDecision(BaseModel):
    note: Optional[str] = None


@router.post("/line-items/{item_id}/approve")
async def approve_line_item(item_id: uuid.UUID, payload: ReviewDecision, user=Depends(get_current_user),
                             membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """Einziger Weg nach APPROVED -- immer menschlich ausgeloest. Gesperrt,
    solange der deterministische Check blockierende Punkte meldet ('unklare
    Werte blockiert', Abnahmekriterium Etappe 2)."""
    item = await _get_line_item_or_404(item_id, membership.tenant_id, db)
    if item.review_status not in (MDCReviewStatus.extracted, MDCReviewStatus.needs_review):
        raise HTTPException(400, f"Position im Status '{_enum_val(item.review_status)}' kann nicht freigegeben werden.")
    if _apply_check(item):
        await db.commit()
        blocking = [i["message"] for i in (item.open_issues_json or []) if i.get("blocking")]
        raise HTTPException(409, {"message": "Freigabe gesperrt -- offene Pflichtpunkte zuerst klaeren.", "blocking_issues": blocking})
    item.review_status = MDCReviewStatus.approved
    item.reviewed_by = str(user.id)
    item.reviewed_at = datetime.utcnow()
    item.review_note = payload.note
    version = (await db.execute(select(MDCDocumentVersion).where(MDCDocumentVersion.id == item.document_version_id))).scalar_one()
    await _refresh_version_status(db, version)
    await db.commit()
    await db.refresh(item)
    return _line_item_to_dict(item)


@router.post("/line-items/{item_id}/reject")
async def reject_line_item(item_id: uuid.UUID, payload: ReviewDecision, user=Depends(get_current_user),
                            membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    item = await _get_line_item_or_404(item_id, membership.tenant_id, db)
    if item.review_status not in (MDCReviewStatus.extracted, MDCReviewStatus.needs_review):
        raise HTTPException(400, f"Position im Status '{_enum_val(item.review_status)}' kann nicht abgelehnt werden.")
    if not (payload.note or "").strip():
        raise HTTPException(400, "Bitte einen Ablehnungsgrund angeben.")
    item.review_status = MDCReviewStatus.rejected
    item.reviewed_by = str(user.id)
    item.reviewed_at = datetime.utcnow()
    item.review_note = payload.note.strip()
    version = (await db.execute(select(MDCDocumentVersion).where(MDCDocumentVersion.id == item.document_version_id))).scalar_one()
    await _refresh_version_status(db, version)
    await db.commit()
    await db.refresh(item)
    return _line_item_to_dict(item)
