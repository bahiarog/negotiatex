"""
Phase 1 API: cases, document upload + synchronous extraction, read-only
comparison (policy checks + potential savings).

Deliberately NOT in scope here (Phase 2+): sending anything to a supplier,
binding an approval to a sent action, the Action Gateway, webhook handling,
an outbox/delivery worker, or any negotiation/counter-offer generation.

Tenant isolation: every handler resolves tenant_id from the authenticated
user's membership (see deps.py) and every query is filtered by it. A case
whose tenant_id does not match the caller's membership is treated as 404,
not 403, to avoid confirming the resource's existence to the wrong tenant.
"""
import hashlib
import logging
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from deps import get_current_user, get_current_membership
from models_v2 import (
    Case, CaseStatus, CaseEvent, Document, LineItem, Policy, PolicyCheck, SavingsRecord,
)
from services import line_item_extraction
from services.policy_engine import evaluate_policy, compute_potential_savings

router = APIRouter()
logger = logging.getLogger(__name__)

UPLOAD_DIR = Path("/app/uploads/documents")
MAX_FILE_SIZE = 25 * 1024 * 1024
ALLOWED_EXT = {".pdf", ".csv"}

# Fields a line_item needs before a meaningful policy check is possible.
REQUIRED_FIELDS_FOR_POLICY = ("unit_price", "quantity", "currency")


class CaseCreate(BaseModel):
    title: str
    category: str


def _case_to_dict(c: Case) -> dict:
    return {
        "id": str(c.id),
        "title": c.title,
        "category": c.category,
        "status": c.status.value if hasattr(c.status, "value") else c.status,
        "case_version": c.case_version,
        "created_at": c.created_at,
        "updated_at": c.updated_at,
    }


async def _transition(db: AsyncSession, case: Case, new_status: CaseStatus, actor: str, reason: str):
    old = case.status
    old_value = old.value if hasattr(old, "value") else old
    case.status = new_status
    case.case_version = (case.case_version or 1) + 1
    case.updated_at = datetime.utcnow()
    db.add(CaseEvent(
        case_id=case.id, tenant_id=case.tenant_id,
        from_status=old_value, to_status=new_status.value,
        actor=actor, reason=reason,
    ))


async def _get_case_or_404(case_id: str, tenant_id, db: AsyncSession) -> Case:
    try:
        cid = uuid.UUID(case_id)
    except ValueError:
        raise HTTPException(404, "Fall nicht gefunden.")
    r = await db.execute(select(Case).where(Case.id == cid))
    case = r.scalar_one_or_none()
    if not case or case.tenant_id != tenant_id:
        raise HTTPException(404, "Fall nicht gefunden.")
    return case


@router.post("/cases")
async def create_case(
    payload: CaseCreate,
    user=Depends(get_current_user),
    membership=Depends(get_current_membership),
    db: AsyncSession = Depends(get_db),
):
    case = Case(
        tenant_id=membership.tenant_id, title=payload.title, category=payload.category,
        status=CaseStatus.RECEIVED, case_version=1,
    )
    db.add(case)
    await db.flush()
    db.add(CaseEvent(
        case_id=case.id, tenant_id=case.tenant_id,
        from_status=None, to_status=CaseStatus.RECEIVED.value,
        actor=str(user.id), reason="Fall erstellt.",
    ))
    await db.commit()
    await db.refresh(case)
    return _case_to_dict(case)


@router.get("/cases")
async def list_cases(membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    r = await db.execute(
        select(Case).where(Case.tenant_id == membership.tenant_id).order_by(Case.created_at.desc())
    )
    return [_case_to_dict(c) for c in r.scalars().all()]


@router.get("/cases/{case_id}")
async def get_case(case_id: str, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    case = await _get_case_or_404(case_id, membership.tenant_id, db)
    return _case_to_dict(case)


@router.get("/cases/{case_id}/events")
async def get_case_events(case_id: str, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    case = await _get_case_or_404(case_id, membership.tenant_id, db)
    r = await db.execute(select(CaseEvent).where(CaseEvent.case_id == case.id).order_by(CaseEvent.created_at))
    return [
        {
            "id": str(e.id), "from_status": e.from_status, "to_status": e.to_status,
            "actor": e.actor, "reason": e.reason, "created_at": e.created_at,
        }
        for e in r.scalars().all()
    ]


@router.post("/cases/{case_id}/documents")
async def upload_document(
    case_id: str,
    file: UploadFile = File(...),
    user=Depends(get_current_user),
    membership=Depends(get_current_membership),
    db: AsyncSession = Depends(get_db),
):
    case = await _get_case_or_404(case_id, membership.tenant_id, db)

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXT:
        raise HTTPException(400, f"Dateityp nicht unterstuetzt. Erlaubt: {sorted(ALLOWED_EXT)}")

    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(400, "Datei zu gross (max. 25 MB).")

    doc_id = uuid.uuid4()
    file_hash = hashlib.sha256(content).hexdigest()
    tenant_dir = UPLOAD_DIR / str(membership.tenant_id)
    tenant_dir.mkdir(parents=True, exist_ok=True)
    safe_name = (file.filename or "upload").replace("/", "_").replace("\\", "_")
    file_path = tenant_dir / f"{doc_id}_{safe_name}"
    file_path.write_bytes(content)

    document = Document(
        id=doc_id, tenant_id=membership.tenant_id, case_id=case.id,
        object_path=str(file_path), file_hash=file_hash,
        original_filename=file.filename, mime_type=file.content_type, version=1,
    )
    db.add(document)
    await db.flush()

    await _transition(db, case, CaseStatus.EXTRACTING, actor=str(user.id), reason=f"Dokument hochgeladen: {file.filename}")
    await db.flush()

    extracted: list[dict] = []
    try:
        if suffix == ".csv":
            extracted = line_item_extraction.extract_line_items_from_csv(str(file_path))
        else:
            from services.pdf_parser import extract_text
            text = await extract_text(str(file_path), file.filename or "document.pdf")
            extracted = line_item_extraction.extract_line_items_from_pdf_text(text)
    except Exception as e:
        logger.error(f"Extraktion fehlgeschlagen fuer Case {case.id}: {e}")
        extracted = []

    has_complete_item = False
    for item in extracted:
        li = LineItem(
            document_id=document.id, tenant_id=membership.tenant_id,
            position_nr=item.get("position_nr"), description=item.get("description"),
            quantity=item.get("quantity"), unit=item.get("unit"),
            unit_price=item.get("unit_price"), net_total=item.get("net_total"),
            currency=item.get("currency"), tax_rate=item.get("tax_rate"),
            contract_period=item.get("contract_period"), cancellation_notice=item.get("cancellation_notice"),
            raw_extracted_json={k: (str(v) if isinstance(v, Decimal) else v) for k, v in item.items()},
            confidence=item.get("confidence"),
        )
        db.add(li)
        if all(item.get(f) is not None for f in REQUIRED_FIELDS_FOR_POLICY):
            has_complete_item = True

    if not extracted or not has_complete_item:
        await _transition(
            db, case, CaseStatus.NEEDS_DATA, actor="system",
            reason="Extraktion unvollstaendig - erforderliche Felder (Einzelpreis/Menge/Waehrung) fehlen bei mindestens einer Position.",
        )
    else:
        await _transition(
            db, case, CaseStatus.REVIEW_REQUIRED, actor="system",
            reason="Extraktion abgeschlossen - manuelle Pruefung erforderlich.",
        )

    await db.commit()
    await db.refresh(document)
    return {
        "document_id": str(document.id),
        "case_status": case.status.value,
        "line_items_extracted": len(extracted),
    }


@router.get("/cases/{case_id}/comparison")
async def get_comparison(case_id: str, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    case = await _get_case_or_404(case_id, membership.tenant_id, db)

    r = await db.execute(
        select(Policy)
        .where(Policy.tenant_id == membership.tenant_id, Policy.is_active == True)  # noqa: E712
        .order_by(Policy.version.desc())
    )
    policy = r.scalars().first()

    r = await db.execute(select(Document).where(Document.case_id == case.id))
    documents = r.scalars().all()
    doc_ids = [d.id for d in documents]

    line_items = []
    if doc_ids:
        r = await db.execute(select(LineItem).where(LineItem.document_id.in_(doc_ids)))
        line_items = r.scalars().all()

    # Idempotent re-evaluation for this case: delete-and-reinsert (fine for Phase 1 volumes).
    await db.execute(delete(PolicyCheck).where(PolicyCheck.case_id == case.id))
    await db.execute(delete(SavingsRecord).where(SavingsRecord.case_id == case.id))

    results = []
    total_potential = Decimal("0")

    for li in line_items:
        outcome = None
        if policy:
            outcome = evaluate_policy(li, policy)
            db.add(PolicyCheck(
                case_id=case.id, tenant_id=membership.tenant_id, policy_id=policy.id,
                policy_version=policy.version, document_id=li.document_id, line_item_id=li.id,
                result=outcome.result, message=outcome.message,
            ))

        benchmark_value = None
        if policy and policy.rules_json:
            benchmark_value = policy.rules_json.get("benchmark_unit_price")
        potential = compute_potential_savings(li, benchmark_value)
        if potential > 0:
            db.add(SavingsRecord(
                tenant_id=membership.tenant_id, case_id=case.id, category=case.category,
                potential_amount=potential, currency=li.currency or "EUR",
                evidence_document_id=li.document_id,
            ))
            total_potential += potential

        results.append({
            "line_item_id": str(li.id),
            "description": li.description,
            "quantity": str(li.quantity) if li.quantity is not None else None,
            "unit": li.unit,
            "unit_price": str(li.unit_price) if li.unit_price is not None else None,
            "net_total": str(li.net_total) if li.net_total is not None else None,
            "currency": li.currency,
            "result": outcome.result if outcome else "n/a",
            "message": outcome.message if outcome else "Keine aktive Policy fuer diesen Mandanten hinterlegt.",
            "potential_savings": str(potential),
        })

    await db.commit()
    return {
        "case_id": str(case.id),
        "policy_name": policy.name if policy else None,
        "policy_version": policy.version if policy else None,
        "items": results,
        "total_potential_savings": str(total_potential),
    }
