"""
Public API -- no authentication required.
Supplier portal: external parties can submit offers directly.
"""
import os, uuid, logging
from pathlib import Path
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from database import get_db
from models import Client, Audit, Offer, ActivityLog

router = APIRouter()
logger = logging.getLogger(__name__)
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/app/uploads/offers"))
MAX_FILE_SIZE = 25 * 1024 * 1024
ALLOWED_EXT = {".pdf", ".xlsx", ".xls", ".csv", ".doc", ".docx"}

@router.get("/info")
async def portal_info():
    return {
        "service": "NegotiateX Supplier Portal",
        "version": "3.0.0",
        "accepts": list(ALLOWED_EXT),
        "max_file_mb": 25,
        "message": "Submit your offer for AI-powered procurement analysis."
    }

@router.post("/submit")
async def supplier_submit(
    company_name: str = Form(...),
    contact_email: str = Form(...),
    contact_name: str = Form(default=""),
    category: str = Form(default="General"),
    total_net: float = Form(default=0.0),
    notes: str = Form(default=""),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """Submit an offer via the supplier portal (no auth required)."""
    from sqlalchemy import select
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXT:
        raise HTTPException(400, f"File type not allowed. Use: {str(ALLOWED_EXT)}")

    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(400, "File too large (max 25 MB)")

    r = await db.execute(select(Client).where(Client.email == contact_email.lower()))
    client = r.scalar_one_or_none()
    if not client:
        client = Client(
            company_name=company_name,
            email=contact_email.lower(),
            contact_name=contact_name,
            industry=category,
        )
        db.add(client)
        await db.flush()

    audit = Audit(
        client_id=client.id,
        status="pending",
        questionnaire={
            "company_name": company_name,
            "industry": category,
            "source": "supplier_portal",
            "contact_name": contact_name,
            "notes": notes,
        }
    )
    db.add(audit)
    await db.flush()

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    file_id = str(uuid.uuid4())
    file_path = UPLOAD_DIR / f"{file_id}{suffix}"
    file_path.write_bytes(content)

    offer = Offer(
        audit_id=audit.id,
        title=file.filename,
        category=category,
        total_net=total_net,
        pdf_file_path=str(file_path),
        pdf_file_name=file.filename,
        pdf_file_size=len(content),
    )
    db.add(offer)
    await db.flush()

    try:
        from services.pdf_parser import extract_text
        offer.parsed_text = extract_text(str(file_path))
    except Exception:
        offer.parsed_text = ""

    await db.commit()

    from routers.audit import _run_ai_task
    import asyncio
    asyncio.create_task(_run_ai_task(str(offer.id)))

    db.add(ActivityLog(
        audit_id=audit.id, entity_type="supplier_portal", entity_id=str(offer.id),
        action="offer.submitted",
        message=f"Supplier portal submission by {company_name}",
        user_name=contact_name or company_name
    ))
    await db.commit()

    logger.info(f"Supplier portal submission: {company_name} ({contact_email}) -- {file.filename}")

    return {
        "status": "received",
        "audit_id": str(audit.id),
        "offer_id": str(offer.id),
        "message": f"Ihr Angebot wurde empfangen und wird analysiert. Audit-ID: {audit.id}"
    }
