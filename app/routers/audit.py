import os, uuid, logging
from datetime import datetime
from pathlib import Path
from typing import Optional
from fastapi import Request, APIRouter, Depends, HTTPException, UploadFile, File, Form, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from database import get_db, AsyncSessionLocal
from models import Client, Audit, Offer, AICheck, Supplier, ActivityLog, OfferStatus
from services.pdf_parser import extract_text
from services.ai_analyzer import run_analysis, extract_total_savings
from services.cache import get_cached_analysis, set_cached_analysis
from services.compliance import validate_analysis

router = APIRouter()
logger = logging.getLogger(__name__)
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/app/uploads/offers"))
MAX_FILE_SIZE = 25 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".xlsx", ".xls", ".csv", ".doc", ".docx"}

class QuestionnaireSubmit(BaseModel):
    company_name: str; industry: str; annual_volume: Optional[float] = None
    biggest_challenge: Optional[str] = None; supplier_count: Optional[int] = None
    primary_categories: Optional[list[str]] = None; email: Optional[str] = None

async def _log(db, action, message, entity_type="offer", entity_id="", audit_id=None, user_name="System"):
    db.add(ActivityLog(audit_id=audit_id, entity_type=entity_type, entity_id=entity_id, action=action, message=message, user_name=user_name))
    await db.commit()

async def _get_or_create_supplier(db, client_id, name, category):
    if not name: return None
    r = await db.execute(select(Supplier).where(Supplier.client_id == client_id, Supplier.name == name))
    s = r.scalar_one_or_none()
    if not s:
        s = Supplier(client_id=client_id, name=name, category=category)
        db.add(s); await db.flush()
    return str(s.id)

async def _run_ai_task(offer_id: str):
    async with AsyncSessionLocal() as db:
        try:
            r = await db.execute(select(Offer).where(Offer.id == offer_id))
            offer = r.scalar_one_or_none()
            if not offer: return
            offer.status = OfferStatus.analyzing
            await db.commit()
            questionnaire = None
            if offer.audit_id:
                ar = await db.execute(select(Audit).where(Audit.id == offer.audit_id))
                audit = ar.scalar_one_or_none()
                if audit: questionnaire = audit.questionnaire

            offer_text = offer.parsed_text or ""
            category = offer.category or "General"
            total = float(offer.total_net or 0)

            # Check cache first
            analysis = await get_cached_analysis(offer_text, category, total)
            if not analysis:
                analysis = await run_analysis(offer_text, category, total, questionnaire=questionnaire)
                await set_cached_analysis(offer_text, category, total, analysis)

            # Compliance gate
            offer_summary = f"{offer.title or 'Offer'} | {category} | EUR{total:,.0f}"
            compliance = await validate_analysis(analysis, offer_summary)
            if not compliance.get("passed") and compliance.get("risk_level") == "high":
                logger.warning(f"Compliance gate FAILED for offer {offer_id}: {compliance.get('flags')}")
            # Store compliance result in analysis
            analysis["compliance"] = compliance

            for ct, cd in analysis["checks"].items():
                db.add(AICheck(offer_id=offer_id, check_type=ct, score=cd["score"], result=cd["result"],
                    critical_count=cd["critical_count"], warning_count=cd["warning_count"],
                    ok_count=cd["ok_count"], raw_response=cd.get("raw_response", "")))
            realistic, max_s = extract_total_savings(analysis)
            offer.ai_score = analysis["aggregate_score"]
            offer.ai_analysis = analysis
            # Auto-extract benchmarks
            try:
                sup = offer.supplier_id
                await extract_benchmarks_from_analysis(
                    offer_id=offer_id, audit_id=str(offer.audit_id or ""),
                    category=offer.category or "General",
                    analysis_result=analysis, supplier_name=None, db=db
                )
            except Exception as be:
                logger.warning(f"Benchmark extraction failed: {be}")
            offer.status = OfferStatus.analyzed
            offer.analyzed_at = datetime.utcnow()
            # Broadcast real-time status update via WebSocket
            try:
                from routers.ws import broadcast as ws_broadcast
                await ws_broadcast(offer_id, {
                    "type": "analysis_complete",
                    "offer_id": offer_id,
                    "status": "analyzed",
                    "score": analysis.get("aggregate_score", 0),
                })
            except Exception as ws_err:
                logger.warning(f"WS broadcast failed: {ws_err}")
            # Fire outbound webhook for ERP/external systems
            try:
                from routers.webhooks import fire_webhook
                await fire_webhook("offer.analyzed", {
                    "offer_id": offer_id,
                    "score": analysis.get("aggregate_score", 0),
                    "category": offer.category,
                    "total_net": float(offer.total_net or 0),
                })
            except Exception as wh_err:
                logger.warning(f"Webhook fire failed: {wh_err}")
            if offer.audit_id:
                ar = await db.execute(select(Audit).where(Audit.id == offer.audit_id))
                audit = ar.scalar_one_or_none()
                if audit:
                    audit.total_savings_identified = (audit.total_savings_identified or 0) + realistic
                    if offer.total_net and float(offer.total_net) > 0:
                        audit.savings_percentage = (realistic / float(offer.total_net)) * 100
            await db.commit()
            logger.info(f"AI done for offer {offer_id} -- score: {analysis['aggregate_score']}")
        except Exception as e:
            logger.error(f"AI task failed for {offer_id}: {e}")
            r = await db.execute(select(Offer).where(Offer.id == offer_id))
            o = r.scalar_one_or_none()
            if o: o.status = OfferStatus.uploaded; await db.commit()

@router.post("/submit")
async def submit(payload: QuestionnaireSubmit, db: AsyncSession = Depends(get_db)):
    email = payload.email or f"contact@{payload.company_name.lower().replace(' ','')}.com"
    r = await db.execute(select(Client).where(Client.email == email))
    client = r.scalar_one_or_none()
    if not client:
        client = Client(company_name=payload.company_name, email=email, industry=payload.industry, annual_volume=payload.annual_volume)
        db.add(client); await db.flush()
    audit = Audit(client_id=client.id, questionnaire=payload.model_dump(), status="pending")
    db.add(audit); await db.commit(); await db.refresh(audit)
    return {"audit_id": str(audit.id), "client_id": str(client.id), "status": "pending"}

@router.post("/upload-offer")
async def upload_offer(background_tasks: BackgroundTasks, audit_id: str = Form(...),
    supplier_name: str = Form(default=""), category: str = Form(default="General"),
    job_number: str = Form(default=""), auto_analyze: bool = Form(default=True),
    file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Audit).where(Audit.id == audit_id))
    audit = r.scalar_one_or_none()
    if not audit: raise HTTPException(404, "Audit not found")
    ext = Path(file.filename or "file.pdf").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS: raise HTTPException(400, f"File type not supported: {ext}")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = f"offer_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}{ext}"
    file_path = UPLOAD_DIR / safe_name
    content = await file.read()
    if len(content) > MAX_FILE_SIZE: raise HTTPException(400, "File too large (max 25MB)")
    with open(file_path, "wb") as f: f.write(content)
    parsed_text = await extract_text(str(file_path), file.filename or safe_name)
    supplier_id = None
    if supplier_name and audit.client_id:
        supplier_id = await _get_or_create_supplier(db, str(audit.client_id), supplier_name, category)
    offer = Offer(audit_id=audit_id, supplier_id=supplier_id, title=file.filename, category=category,
        job_number=job_number, pdf_file_path=str(file_path), pdf_file_name=file.filename,
        pdf_file_size=len(content), parsed_text=parsed_text, status=OfferStatus.uploaded)
    db.add(offer); await db.flush(); offer_id = str(offer.id)
    await _log(db, "uploaded", f"Document '{file.filename}' uploaded ({len(content)//1024}KB)", audit_id=audit_id, entity_id=offer_id)
    if auto_analyze:
        offer.status = OfferStatus.analyzing
        await db.commit()
        background_tasks.add_task(_run_ai_task, offer_id)
    else:
        await db.commit()
    return {"offer_id": offer_id, "file_name": file.filename, "status": offer.status.value, "auto_analyze": auto_analyze}

@router.post("/analyze/{offer_id}")
async def trigger_analysis(offer_id: str, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Offer).where(Offer.id == offer_id))
    offer = r.scalar_one_or_none()
    if not offer: raise HTTPException(404, "Offer not found")
    if offer.status == OfferStatus.analyzing: return {"message": "Already analyzing"}
    offer.status = OfferStatus.analyzing; await db.commit()
    background_tasks.add_task(_run_ai_task, offer_id)
    return {"offer_id": offer_id, "status": "analyzing"}

@router.get("/audit/{audit_id}")
async def get_audit(audit_id: str, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Audit).where(Audit.id == audit_id))
    audit = r.scalar_one_or_none()
    if not audit: raise HTTPException(404, "Audit not found")
    offers = (await db.execute(select(Offer).where(Offer.audit_id == audit_id))).scalars().all()
    offers_data = []
    for o in offers:
        checks = (await db.execute(select(AICheck).where(AICheck.offer_id == o.id))).scalars().all()
        offers_data.append({"id": str(o.id), "title": o.title, "category": o.category, "total_net": o.total_net,
            "status": o.status.value if o.status else "uploaded", "ai_score": o.ai_score,
            "checks": [{"check_type": c.check_type.value if c.check_type else c.check_type, "score": c.score, "result": c.result, "critical_count": c.critical_count, "warning_count": c.warning_count, "ok_count": c.ok_count} for c in checks]})
    return {"id": str(audit.id), "status": audit.status, "total_savings_identified": audit.total_savings_identified, "savings_percentage": audit.savings_percentage, "offers": offers_data}

@router.get("/offer/{offer_id}/checks")
async def get_checks(offer_id: str, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Offer).where(Offer.id == offer_id))
    offer = r.scalar_one_or_none()
    if not offer: raise HTTPException(404, "Offer not found")
    checks = (await db.execute(select(AICheck).where(AICheck.offer_id == offer_id))).scalars().all()
    return {"offer_id": offer_id, "ai_score": offer.ai_score, "status": offer.status.value if offer.status else "uploaded",
        "checks": [{"check_type": c.check_type.value if c.check_type else c.check_type, "score": c.score, "result": c.result, "critical_count": c.critical_count, "warning_count": c.warning_count, "ok_count": c.ok_count} for c in checks]}




@router.get("/list")
async def list_audits(request: Request, db: AsyncSession = Depends(get_db)):
    from routers.auth import verify_token
    from fastapi import Request
    auth = request.headers.get("Authorization","")
    try:
        payload = verify_token(auth)
        user_email = payload.get("email","")
        is_admin = payload.get("is_admin", False)
    except:
        user_email = ""
        is_admin = False
    result = await db.execute(select(Audit).order_by(Audit.created_at.desc()))
    audits = result.scalars().all()
    out = []
    for a in audits:
        # Filter by user email unless admin
        q = a.questionnaire or {}
        if not is_admin and q.get("email","") != user_email:
            continue
        offers_r = await db.execute(select(Offer).where(Offer.audit_id == a.id))
        offers = offers_r.scalars().all()
        # Get checks for each offer
        offers_data = []
        for o in offers:
            checks_r = await db.execute(select(AICheck).where(AICheck.offer_id == o.id))
            checks = checks_r.scalars().all()
            offers_data.append({
                "id": str(o.id),
                "title": o.pdf_file_name or o.title,
                "category": o.category,
                "job_number": o.job_number,
                "total_net": o.total_net,
                "status": o.status.value if o.status else "uploaded",
                "ai_score": o.ai_score,
                "supplier_name": None,
                "checks": [{"check_type": c.check_type.value if c.check_type else c.check_type, "score": c.score, "result": c.result, "critical_count": c.critical_count, "warning_count": c.warning_count, "ok_count": c.ok_count} for c in checks],
            })
        out.append({
            "id": str(a.id),
            "status": a.status,
            "questionnaire": a.questionnaire,
            "total_savings_identified": a.total_savings_identified,
            "savings_percentage": a.savings_percentage,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "offers": offers_data,
        })
    return out


async def extract_benchmarks_from_analysis(
    offer_id: str, audit_id: str, category: str,
    analysis_result: dict, supplier_name: str, db
):
    """Auto-extract benchmark values from AI analysis results into the pool."""
    from models import BenchmarkEntry
    import re
    entries = []
    checks = analysis_result.get("checks", {})

    # Extract from pricing check
    pricing = checks.get("pricing", {}).get("result", {})
    for bm in pricing.get("benchmarks", []):
        # Try to parse market range value
        market_str = bm.get("market_range", "")
        nums = re.findall(r"[\d,\.]+", market_str.replace(",", "."))
        if nums:
            try:
                val = float(nums[0].replace(",", "."))
                if val > 0:
                    entries.append(BenchmarkEntry(
                        category=category,
                        metric_type=_guess_metric_type(bm.get("metric",""), val),
                        metric_label=bm.get("metric", ""),
                        value=val, source="offer",
                        offer_id=offer_id, audit_id=audit_id,
                        supplier_anonymized=(supplier_name or "")[:3].upper() if supplier_name else None,
                    ))
            except: pass

    # Extract from savings negotiation points
    savings = checks.get("savings", {}).get("result", {})
    for pt in savings.get("negotiation_points", []):
        if pt.get("potential_saving", 0) > 0:
            entries.append(BenchmarkEntry(
                category=category,
                metric_type="savings_point",
                metric_label=pt.get("point","")[:100],
                value=float(pt.get("potential_saving", 0)),
                source="offer", offer_id=offer_id, audit_id=audit_id,
            ))

    for e in entries:
        db.add(e)
    if entries:
        await db.commit()

def _guess_metric_type(metric_str: str, value: float) -> str:
    s = metric_str.lower()
    if any(x in s for x in ["stunde", "hour", "h/", "/h"]): return "hourly_rate"
    if any(x in s for x in ["tag", "day", "daily"]): return "daily_rate"
    if any(x in s for x in ["fte", "vollzeit", "monat", "month"]): return "monthly"
    if any(x in s for x in ["lead", "cpl"]): return "cpl"
    if value < 300: return "hourly_rate"
    if value < 2000: return "daily_rate"
    return "flat"


# -- Counter-Offer / Negotiator Agent --
@router.post("/counter-offer/{offer_id}")
async def generate_counter_offer(offer_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Generate a professional counter-offer package using Claude."""
    from routers.auth import verify_token
    auth = request.headers.get("Authorization", "")
    payload = verify_token(auth)

    r = await db.execute(select(Offer).where(Offer.id == offer_id))
    offer = r.scalar_one_or_none()
    if not offer:
        raise HTTPException(404, "Offer not found")

    if offer.status not in [OfferStatus.analyzed, OfferStatus.po_created]:
        raise HTTPException(400, "Offer must be analyzed before generating a counter-offer")

    # Load AI checks
    checks_r = await db.execute(select(AICheck).where(AICheck.offer_id == offer_id))
    checks = {c.check_type: {"score": c.score, "result": c.result} for c in checks_r.scalars().all()}

    if not checks:
        raise HTTPException(400, "No analysis found. Please run the AI analysis first.")

    # Get supplier name if available
    supplier_name = ""
    if offer.supplier_id:
        sr = await db.execute(select(Supplier).where(Supplier.id == offer.supplier_id))
        s = sr.scalar_one_or_none()
        if s:
            supplier_name = s.name

    from services.negotiator import generate_counter_offer
    result = await generate_counter_offer(
        title=offer.title or "Offer",
        category=offer.category or "General",
        total_net=float(offer.total_net or 0),
        supplier_name=supplier_name,
        analysis_checks=checks,
        language="de",
    )

    await _log(db, "COUNTER_OFFER_GENERATED",
               f"Counter-offer generated: target EUR{result.get('target_price', 0):,.2f}",
               entity_id=offer_id, audit_id=str(offer.audit_id) if offer.audit_id else None)

    return {
        "offer_id": offer_id,
        "offer_title": offer.title,
        "original_total": float(offer.total_net or 0),
        **result
    }


@router.get("/counter-offer/{offer_id}/cached")
async def get_cached_counter_offer(offer_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Return cached counter-offer from activity log if available."""
    from routers.auth import verify_token
    auth = request.headers.get("Authorization", "")
    verify_token(auth)
    # For now, always regenerate -- caching can be added later
    raise HTTPException(404, "No cached counter-offer. Use POST to generate.")

@router.post("/send-email/{offer_id}")
async def send_negotiation_email_endpoint(offer_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Send the AI-generated negotiation email to the supplier."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    to_email = body.get("to_email", "")
    subject = body.get("subject", "")
    email_body = body.get("body", "")

    if not to_email or not subject or not email_body:
        raise HTTPException(status_code=400, detail="to_email, subject, body required")

    from services.email_sender import send_negotiation_email
    result = send_negotiation_email(to_email=to_email, subject=subject, body=email_body)

    # Log the action
    try:
        db.add(ActivityLog(
            audit_id=None, entity_type="email", entity_id=offer_id,
            action="email.sent" if result["sent"] else "email.failed",
            message=f"To: {to_email} | {result['message']}", user_name="System"
        ))
        await db.commit()
    except Exception:
        pass

    return result
