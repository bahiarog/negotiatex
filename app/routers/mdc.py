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
import hashlib
import logging
import os
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from deps import get_current_user, get_current_membership
from models_mdc import MDCCategory, MDCSupplier, MDCDocument, MDCDocumentVersion, MDCImportStatus, MDCDocumentType
from services.pdf_parser import extract_text

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
    category_id: Optional[str] = Form(None),
    supplier_name: Optional[str] = Form(None),
    document_type: str = Form("other"),
    title: Optional[str] = Form(None),
    source: Optional[str] = Form(None),
    rights_note: Optional[str] = Form(None),
    revision_of_document_id: Optional[str] = Form(None),
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
async def get_document(document_id: str, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    doc = await _get_document_or_404(document_id, membership.tenant_id, db)
    versions = (await db.execute(select(MDCDocumentVersion).where(MDCDocumentVersion.document_id == doc.id).order_by(MDCDocumentVersion.version_number))).scalars().all()
    return _document_to_dict(doc, versions)


@router.get("/documents/{document_id}/versions/{version_id}/text")
async def get_version_text(document_id: str, version_id: str, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    doc = await _get_document_or_404(document_id, membership.tenant_id, db)
    r = await db.execute(select(MDCDocumentVersion).where(MDCDocumentVersion.id == version_id, MDCDocumentVersion.document_id == doc.id))
    v = r.scalar_one_or_none()
    if not v:
        raise HTTPException(404, "Version nicht gefunden.")
    return {"extracted_text": v.extracted_text, "import_status": v.import_status.value, "import_error": v.import_error}


@router.get("/documents/{document_id}/versions/{version_id}/download")
async def download_version(document_id: str, version_id: str, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    """Sichere, authentifizierte Download-Route statt frei waehlbarer
    Dateipfade -- das Original wird nie ueber einen direkt erratbaren Pfad
    ausgeliefert, sondern nur ueber Version-ID nach Mandantenpruefung."""
    doc = await _get_document_or_404(document_id, membership.tenant_id, db)
    r = await db.execute(select(MDCDocumentVersion).where(MDCDocumentVersion.id == version_id, MDCDocumentVersion.document_id == doc.id))
    v = r.scalar_one_or_none()
    if not v or not os.path.exists(v.file_path):
        raise HTTPException(404, "Originaldatei nicht gefunden.")
    return FileResponse(v.file_path, filename=v.file_name)
