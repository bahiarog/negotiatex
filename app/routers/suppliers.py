import hashlib, secrets, logging, base64
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional
from database import get_db
from models import Supplier, SupplierInvite

router = APIRouter()
logger = logging.getLogger(__name__)

COMPLIANCE_TEXT = (
    "Ich versichere hiermit, dass die oben gemachten Angaben korrekt und "
    "vollständig sind, und dass ich berechtigt bin, diese Angaben im Namen "
    "des genannten Unternehmens zu übermitteln."
)


class SupplierCreate(BaseModel):
    name: str
    legal_form: Optional[str] = None
    category: Optional[str] = None
    contact_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    website: Optional[str] = None
    address: Optional[str] = None
    country: Optional[str] = "Germany"
    tax_id: Optional[str] = None
    vat_id: Optional[str] = None
    duns_number: Optional[str] = None
    commercial_register_number: Optional[str] = None
    employee_count: Optional[int] = None
    iban: Optional[str] = None
    bic: Optional[str] = None
    bank_name: Optional[str] = None
    withholding_tax_liable: Optional[bool] = False
    payment_terms_days: Optional[int] = None
    notes: Optional[str] = None


def _to_dict(s: Supplier) -> dict:
    return {
        "id": str(s.id), "name": s.name, "legal_form": s.legal_form, "category": s.category,
        "contact_name": s.contact_name, "phone": s.phone, "email": s.email, "website": s.website,
        "address": s.address, "country": s.country, "tax_id": s.tax_id, "vat_id": s.vat_id,
        "duns_number": s.duns_number, "commercial_register_number": s.commercial_register_number,
        "employee_count": s.employee_count, "iban": s.iban, "bic": s.bic, "bank_name": s.bank_name,
        "withholding_tax_liable": s.withholding_tax_liable, "payment_terms_days": s.payment_terms_days,
        "notes": s.notes, "created_at": s.created_at.isoformat() if s.created_at else None,
    }


@router.get("/list")
async def list_suppliers(db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Supplier).order_by(Supplier.created_at.desc()))
    return [_to_dict(s) for s in r.scalars().all()]


@router.post("/create")
async def create_supplier(payload: SupplierCreate, db: AsyncSession = Depends(get_db)):
    if not payload.name.strip():
        raise HTTPException(400, "Unternehmen ist ein Pflichtfeld.")
    s = Supplier(**payload.model_dump())
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return _to_dict(s)


@router.put("/{supplier_id}")
async def update_supplier(supplier_id: str, payload: SupplierCreate, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Supplier).where(Supplier.id == supplier_id))
    s = r.scalar_one_or_none()
    if not s:
        raise HTTPException(404, "Lieferant nicht gefunden.")
    for k, v in payload.model_dump().items():
        setattr(s, k, v)
    await db.commit()
    await db.refresh(s)
    return _to_dict(s)


@router.delete("/{supplier_id}")
async def delete_supplier(supplier_id: str, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Supplier).where(Supplier.id == supplier_id))
    s = r.scalar_one_or_none()
    if not s:
        raise HTTPException(404, "Lieferant nicht gefunden.")
    await db.delete(s)
    await db.commit()
    return {"ok": True}


# ── Self-service onboarding invite ──────────────────────────────────────────
class InvitePayload(BaseModel):
    email: str
    name_hint: Optional[str] = None


@router.post("/invite")
async def invite_supplier(payload: InvitePayload, db: AsyncSession = Depends(get_db)):
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    invite = SupplierInvite(token_hash=token_hash, email=payload.email.lower(), name_hint=payload.name_hint)
    db.add(invite)
    await db.commit()

    from services.email_sender import send_negotiation_email
    link = f"https://negotiatex.ai/supplier-onboarding?token={raw_token}"
    body = (
        f"Hallo{' ' + payload.name_hint if payload.name_hint else ''},\n\n"
        f"wir bitten Sie, Ihre Unternehmensdaten für unsere Lieferantenakte zu hinterlegen. "
        f"Das dauert etwa 5 Minuten. Bitte nutzen Sie dazu den folgenden, persönlichen Link:\n\n"
        f"{link}\n\n"
        f"Der Link ist nur für Sie bestimmt und läuft nach einmaliger Nutzung ab.\n\n"
        f"Vielen Dank für Ihre Mitarbeit!"
    )
    try:
        send_negotiation_email(payload.email, "Bitte Unternehmensdaten hinterlegen — NegotiateX.ai", body)
    except Exception:
        logger.exception("Supplier invite email failed to send")

    return {"message": f"Einladung an {payload.email} gesendet."}


@router.get("/invite/status")
async def invite_status(token: str, db: AsyncSession = Depends(get_db)):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    r = await db.execute(select(SupplierInvite).where(SupplierInvite.token_hash == token_hash))
    inv = r.scalar_one_or_none()
    if not inv:
        raise HTTPException(404, "Dieser Link ist ungültig.")
    if inv.status == "completed":
        raise HTTPException(400, "Dieser Link wurde bereits verwendet.")
    return {"email": inv.email, "name_hint": inv.name_hint, "compliance_text": COMPLIANCE_TEXT}


class InviteSubmitPayload(SupplierCreate):
    token: str
    confirmed_accurate: bool
    confirmed_by_name: str


@router.post("/invite/submit")
async def invite_submit(payload: InviteSubmitPayload, db: AsyncSession = Depends(get_db)):
    if not payload.confirmed_accurate:
        raise HTTPException(400, "Bitte bestätigen Sie die Richtigkeit der Angaben.")
    if not payload.confirmed_by_name.strip():
        raise HTTPException(400, "Bitte Ihren Namen zur Bestätigung angeben.")

    token_hash = hashlib.sha256(payload.token.encode()).hexdigest()
    r = await db.execute(select(SupplierInvite).where(SupplierInvite.token_hash == token_hash))
    inv = r.scalar_one_or_none()
    if not inv:
        raise HTTPException(404, "Dieser Link ist ungültig.")
    if inv.status == "completed":
        raise HTTPException(400, "Dieser Link wurde bereits verwendet.")
    if not payload.name.strip():
        raise HTTPException(400, "Unternehmen ist ein Pflichtfeld.")

    data = payload.model_dump(exclude={"token", "confirmed_accurate", "confirmed_by_name"})
    s = Supplier(**data)
    s.notes = ((s.notes + "\n\n") if s.notes else "") + (
        f"[Selbstauskunft] Bestätigt von {payload.confirmed_by_name} am "
        f"{datetime.utcnow().strftime('%d.%m.%Y %H:%M')} UTC — \"{COMPLIANCE_TEXT}\""
    )
    db.add(s)
    await db.flush()

    inv.status = "completed"
    inv.supplier_id = s.id
    inv.confirmed_by_name = payload.confirmed_by_name
    inv.confirmed_at = datetime.utcnow()
    await db.commit()

    return {"message": "Vielen Dank! Ihre Angaben wurden übermittelt."}


# ── AI extraction from uploaded document / screenshot ───────────────────────
EXTRACTION_SYSTEM_PROMPT = """Du extrahierst Lieferanten-Stammdaten aus einem Dokument oder Screenshot \
(z. B. Briefkopf, Impressum, Handelsregisterauszug, Visitenkarte, Rechnung).

Gib AUSSCHLIESSLICH ein JSON-Objekt zurück, exakt mit diesen Feldern (keine zusätzlichen Felder, kein Freitext davor/danach):
{"name": null, "legal_form": null, "category": null, "contact_name": null, "phone": null, "email": null,
 "website": null, "address": null, "country": null, "tax_id": null, "vat_id": null, "duns_number": null,
 "commercial_register_number": null, "employee_count": null, "iban": null, "bic": null, "bank_name": null}

Regeln:
- Trage NUR Werte ein, die im Dokument eindeutig erkennbar sind. Bei Unsicherheit oder Nichtvorhandensein: null.
- Erfinde NIEMALS plausibel klingende Werte.
- "tax_id" ist die Steuernummer, "vat_id" die USt-IdNr. (Format DE + 9 Ziffern) — nicht verwechseln.
- "commercial_register_number" im Format wie "HRB 12345" oder "HRA 12345".
- "employee_count" nur als Zahl, falls explizit genannt.
- country als Ländername auf Deutsch (z. B. "Germany", "Austria")."""


def _extract_json(text: str) -> dict:
    import json, re
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        text = m.group(0)
    return json.loads(text)


@router.post("/extract")
async def extract_supplier_data(file: UploadFile = File(...)):
    import anthropic
    from pathlib import Path

    content = await file.read()
    if len(content) > 15 * 1024 * 1024:
        raise HTTPException(400, "Datei zu groß (max. 15 MB).")

    suffix = Path(file.filename or "").suffix.lower()
    image_types = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}

    client = anthropic.Anthropic()
    try:
        if suffix in image_types:
            b64 = base64.standard_b64encode(content).decode()
            user_content = [
                {"type": "image", "source": {"type": "base64", "media_type": image_types[suffix], "data": b64}},
                {"type": "text", "text": "Extrahiere die Lieferanten-Stammdaten aus diesem Bild gemäß Systemanweisung."},
            ]
        else:
            import tempfile, os as _os
            with tempfile.NamedTemporaryFile(suffix=suffix or ".pdf", delete=False) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            try:
                from services.pdf_parser import extract_text
                text = await extract_text(tmp_path, file.filename or "document.pdf")
            finally:
                _os.unlink(tmp_path)
            if not text or text.startswith("[Error") or text.startswith("[Unsupported"):
                raise HTTPException(400, f"Datei konnte nicht gelesen werden: {text}")
            user_content = [{"type": "text", "text": f"Dokumenttext:\n\n{text[:12000]}\n\nExtrahiere die Lieferanten-Stammdaten gemäß Systemanweisung."}]

        response = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=1024,
            thinking={"type": "disabled"},
            system=EXTRACTION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        raw_text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
        data = _extract_json(raw_text)
        return {"extracted": data}
    except HTTPException:
        raise
    except Exception:
        logger.exception("Supplier data extraction failed")
        raise HTTPException(503, "KI-Extraktion momentan nicht möglich. Bitte Felder manuell ausfüllen.")
