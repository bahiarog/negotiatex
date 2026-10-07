import base64, logging, re, json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from database import get_db
from models import POItem, PurchaseOrder
from models_invoices import Invoice, InvoiceLineItem

router = APIRouter()
logger = logging.getLogger(__name__)

UPLOAD_DIR = Path("/app/uploads/invoices")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ── AI extraction ────────────────────────────────────────────────────────────
# Same philosophy as suppliers.py's EXTRACTION_SYSTEM_PROMPT: never fabricate,
# null for anything unclear. Extraction only ever lands data in a review state
# (received/extracted) -- it never marks an invoice approved or paid.
EXTRACTION_SYSTEM_PROMPT = """Du extrahierst Rechnungsdaten aus einem Dokument (z. B. PDF-Rechnung, Scan, Screenshot).

Gib AUSSCHLIESSLICH ein JSON-Objekt zurück, exakt mit diesen Feldern (keine zusätzlichen Felder, kein Freitext davor/danach):
{"invoice_number": null, "invoice_date": null, "due_date": null, "currency": null,
 "total_net": null, "total_tax": null, "total_gross": null, "supplier_name": null,
 "line_items": [{"description": null, "quantity": null, "unit_price": null, "net_total": null}]}

Regeln:
- Trage NUR Werte ein, die im Dokument eindeutig erkennbar sind. Bei Unsicherheit oder Nichtvorhandensein: null.
- Erfinde NIEMALS plausibel klingende Werte.
- Datumsfelder im Format YYYY-MM-DD, falls erkennbar, sonst null.
- "currency" als ISO-Code (z. B. "EUR", "USD"), falls erkennbar.
- "line_items" ist eine Liste aller Rechnungspositionen. Falls keine Positionen erkennbar sind: leere Liste []."""


def _extract_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        text = m.group(0)
    return json.loads(text)


def _to_decimal(v) -> Optional[Decimal]:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _to_dt(v):
    if not v:
        return None
    from datetime import datetime
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


def _inv_to_dict(inv: Invoice) -> dict:
    return {
        "id": str(inv.id), "supplier_id": str(inv.supplier_id) if inv.supplier_id else None,
        "invoice_number": inv.invoice_number,
        "invoice_date": inv.invoice_date.isoformat() if inv.invoice_date else None,
        "due_date": inv.due_date.isoformat() if inv.due_date else None,
        "currency": inv.currency,
        "total_net": float(inv.total_net) if inv.total_net is not None else None,
        "total_tax": float(inv.total_tax) if inv.total_tax is not None else None,
        "total_gross": float(inv.total_gross) if inv.total_gross is not None else None,
        "purchase_order_id": str(inv.purchase_order_id) if inv.purchase_order_id else None,
        "status": inv.status, "file_name": inv.file_name,
        "uploaded_at": inv.uploaded_at.isoformat() if inv.uploaded_at else None,
    }


@router.post("/upload")
async def upload_invoice(file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    import anthropic, uuid as uuidlib

    content = await file.read()
    if len(content) > 15 * 1024 * 1024:
        raise HTTPException(400, "Datei zu groß (max. 15 MB).")

    suffix = Path(file.filename or "").suffix.lower()
    image_types = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}

    # Save the uploaded file first -- never lose the upload even if extraction fails.
    saved_name = f"{uuidlib.uuid4()}{suffix}"
    saved_path = UPLOAD_DIR / saved_name
    saved_path.write_bytes(content)

    inv = Invoice(status="received", file_path=str(saved_path), file_name=file.filename)
    db.add(inv)
    await db.flush()

    extracted_text = None
    try:
        client = anthropic.Anthropic()
        if suffix in image_types:
            b64 = base64.standard_b64encode(content).decode()
            user_content = [
                {"type": "image", "source": {"type": "base64", "media_type": image_types[suffix], "data": b64}},
                {"type": "text", "text": "Extrahiere die Rechnungsdaten aus diesem Bild gemäß Systemanweisung."},
            ]
        else:
            from services.pdf_parser import extract_text
            text = await extract_text(str(saved_path), file.filename or "invoice.pdf")
            if not text or text.startswith("[Error") or text.startswith("[Unsupported"):
                raise ValueError(f"Datei konnte nicht gelesen werden: {text}")
            extracted_text = text
            user_content = [{"type": "text", "text": f"Dokumenttext:\n\n{text[:12000]}\n\nExtrahiere die Rechnungsdaten gemäß Systemanweisung."}]

        response = client.messages.create(
            model="claude-sonnet-5", max_tokens=1536,
            system=EXTRACTION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        raw_text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
        data = _extract_json(raw_text)

        inv.invoice_number = data.get("invoice_number")
        inv.invoice_date = _to_dt(data.get("invoice_date"))
        inv.due_date = _to_dt(data.get("due_date"))
        inv.currency = data.get("currency") or "EUR"
        inv.total_net = _to_decimal(data.get("total_net"))
        inv.total_tax = _to_decimal(data.get("total_tax"))
        inv.total_gross = _to_decimal(data.get("total_gross"))
        inv.parsed_text = extracted_text
        inv.status = "extracted"

        for i, li in enumerate(data.get("line_items") or []):
            db.add(InvoiceLineItem(
                invoice_id=inv.id, position_nr=i + 1, description=li.get("description") or "(ohne Beschreibung)",
                quantity=_to_decimal(li.get("quantity")), unit_price=_to_decimal(li.get("unit_price")),
                net_total=_to_decimal(li.get("net_total")), match_status="unmatched",
            ))
    except Exception:
        # Extraction failed entirely -- the invoice row (status=received) and file
        # are already saved, so the human can enter data manually. Never lose the upload.
        logger.exception("Invoice extraction failed")

    await db.commit()
    await db.refresh(inv)
    return _inv_to_dict(inv)


@router.get("/list")
async def list_invoices(db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Invoice).order_by(Invoice.uploaded_at.desc()))
    return [_inv_to_dict(i) for i in r.scalars().all()]


@router.get("/{invoice_id}")
async def get_invoice(invoice_id: str, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Invoice).where(Invoice.id == invoice_id))
    inv = r.scalar_one_or_none()
    if not inv:
        raise HTTPException(404, "Rechnung nicht gefunden.")
    lines = (await db.execute(select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == invoice_id))).scalars().all()

    po_items_by_id = {}
    if any(l.matched_po_item_id for l in lines):
        ids = [l.matched_po_item_id for l in lines if l.matched_po_item_id]
        r2 = await db.execute(select(POItem).where(POItem.id.in_(ids)))
        for pi in r2.scalars().all():
            po_items_by_id[str(pi.id)] = {"description": pi.description, "quantity": pi.quantity,
                                           "unit_price": pi.unit_price, "total_price": pi.total_price}

    out = _inv_to_dict(inv)
    out["line_items"] = [
        {"id": str(l.id), "position_nr": int(l.position_nr) if l.position_nr is not None else None,
         "description": l.description,
         "quantity": float(l.quantity) if l.quantity is not None else None,
         "unit_price": float(l.unit_price) if l.unit_price is not None else None,
         "net_total": float(l.net_total) if l.net_total is not None else None,
         "match_status": l.match_status,
         "matched_po_item": po_items_by_id.get(str(l.matched_po_item_id)) if l.matched_po_item_id else None}
        for l in lines
    ]
    return out


# ── 2-way matching (invoice vs. PO) ──────────────────────────────────────────
# NOTE: this is explicitly a 2-way match, not 3-way. There is no goods-receipt /
# delivery-confirmation concept in this system, so we only ever compare the
# invoice to the purchase order, never to a separate receiving record.

_STOPWORDS = {"the", "a", "an", "and", "or", "of", "for", "to", "in", "und", "der", "die", "das", "für", "von", "mit"}


def _score_description(a: str, b: str) -> float:
    """Simple pure-Python case-insensitive word-overlap scorer (0..1).
    No external fuzzy-matching library -- this is intentionally a basic
    Jaccard-style overlap on word tokens, not real fuzzy/semantic matching,
    so near-miss spellings or reordered/abbreviated descriptions may not match."""
    wa = set(re.findall(r"[a-zA-ZäöüÄÖÜß0-9]+", a.lower())) - _STOPWORDS
    wb = set(re.findall(r"[a-zA-ZäöüÄÖÜß0-9]+", b.lower())) - _STOPWORDS
    if not wa or not wb:
        return 0.0
    if a.strip().lower() in b.strip().lower() or b.strip().lower() in a.strip().lower():
        return 1.0
    overlap = wa & wb
    return len(overlap) / len(wa | wb)


def _within_tolerance(v1: Optional[Decimal], v2: Optional[Decimal], pct=Decimal("0.02")) -> bool:
    if v1 is None or v2 is None:
        return False
    if v2 == 0:
        return v1 == 0
    return abs(v1 - v2) <= abs(v2) * pct


class MatchPayload(BaseModel):
    purchase_order_id: str


@router.post("/{invoice_id}/match")
async def match_invoice(invoice_id: str, payload: MatchPayload, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Invoice).where(Invoice.id == invoice_id))
    inv = r.scalar_one_or_none()
    if not inv:
        raise HTTPException(404, "Rechnung nicht gefunden.")

    r2 = await db.execute(select(PurchaseOrder).where(PurchaseOrder.id == payload.purchase_order_id))
    po = r2.scalar_one_or_none()
    if not po:
        raise HTTPException(404, "Bestellung nicht gefunden.")

    po_items = (await db.execute(select(POItem).where(POItem.purchase_order_id == po.id))).scalars().all()
    lines = (await db.execute(select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == invoice_id))).scalars().all()

    all_ok = True
    for line in lines:
        best_item, best_score = None, 0.0
        for pi in po_items:
            score = _score_description(line.description or "", pi.description or "")
            if score > best_score:
                best_item, best_score = pi, score

        if not best_item or best_score < 0.3:
            line.match_status = "unmatched"
            line.matched_po_item_id = None
            all_ok = False
            continue

        line.matched_po_item_id = best_item.id
        qty_ok = _within_tolerance(line.quantity, Decimal(str(best_item.quantity)) if best_item.quantity is not None else None)
        price_ok = _within_tolerance(line.unit_price, Decimal(str(best_item.unit_price)) if best_item.unit_price is not None else None)

        if qty_ok and price_ok:
            line.match_status = "ok"
        elif not price_ok and qty_ok:
            line.match_status = "price_variance"
            all_ok = False
        elif not qty_ok and price_ok:
            line.match_status = "qty_variance"
            all_ok = False
        else:
            line.match_status = "price_variance"
            all_ok = False

    inv.purchase_order_id = po.id
    inv.status = "matched" if (lines and all_ok) else "exception"
    await db.commit()
    return await get_invoice(invoice_id, db)


@router.post("/{invoice_id}/approve-for-payment")
async def approve_for_payment(invoice_id: str, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Invoice).where(Invoice.id == invoice_id))
    inv = r.scalar_one_or_none()
    if not inv:
        raise HTTPException(404, "Rechnung nicht gefunden.")
    if inv.status != "matched":
        raise HTTPException(400, f"Rechnung ist im Status '{inv.status}'. Bitte zuerst die Abweichung(en) klären, bevor eine Zahlungsfreigabe möglich ist (kein Override für diese Version).")
    inv.status = "approved_for_payment"
    await db.commit()
    return _inv_to_dict(inv)


class RejectPayload(BaseModel):
    reason: str


@router.post("/{invoice_id}/reject")
async def reject_invoice(invoice_id: str, payload: RejectPayload, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Invoice).where(Invoice.id == invoice_id))
    inv = r.scalar_one_or_none()
    if not inv:
        raise HTTPException(404, "Rechnung nicht gefunden.")
    inv.status = "rejected"
    inv.parsed_text = (inv.parsed_text or "") + f"\n\n[Abgelehnt] {payload.reason}"
    await db.commit()
    return _inv_to_dict(inv)
