from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from database import get_db
from models import PurchaseOrder, POItem, Offer, Supplier, POStatus, OfferStatus

router = APIRouter()

class POItemCreate(BaseModel):
    description: str; quantity: float = 1.0; unit: str = "Lump sum"; unit_price: float; total_price: Optional[float] = None

class POCreate(BaseModel):
    offer_id: Optional[str] = None; supplier_id: Optional[str] = None; audit_id: Optional[str] = None
    po_number: Optional[str] = None; job_number: Optional[str] = None; amount_net: float
    tax_rate: float = 19.0; cost_center: Optional[str] = None; orderer_name: Optional[str] = None
    orderer_email: Optional[str] = None; delivery_date: Optional[datetime] = None; notes: Optional[str] = None
    items: list[POItemCreate] = []
    issuer_company: Optional[str] = None; issuer_invoice_address: Optional[str] = None

async def _po_number(db):
    count = (await db.execute(select(func.count(PurchaseOrder.id)))).scalar() or 0
    return f"PO-{datetime.now().year}-{str(count+1).zfill(5)}"

@router.post("/create")
async def create_po(payload: POCreate, db: AsyncSession = Depends(get_db)):
    po_number = payload.po_number or await _po_number(db)
    amount_gross = round(payload.amount_net * (1 + payload.tax_rate / 100), 2)
    po = PurchaseOrder(po_number=po_number, offer_id=payload.offer_id, supplier_id=payload.supplier_id,
        audit_id=payload.audit_id, job_number=payload.job_number, amount_net=payload.amount_net,
        amount_gross=amount_gross, tax_rate=payload.tax_rate, cost_center=payload.cost_center,
        orderer_name=payload.orderer_name, orderer_email=payload.orderer_email,
        delivery_date=payload.delivery_date, notes=payload.notes, status=POStatus.draft)
    db.add(po)
    await db.flush()
    for i, item in enumerate(payload.items):
        db.add(POItem(purchase_order_id=po.id, position_nr=i+1, description=item.description,
            quantity=item.quantity, unit=item.unit, unit_price=item.unit_price,
            total_price=item.total_price or item.quantity * item.unit_price))
    if payload.offer_id:
        r = await db.execute(select(Offer).where(Offer.id == payload.offer_id))
        o = r.scalar_one_or_none()
        if o: o.status = OfferStatus.po_created
    await db.commit()
    return {"po_id": str(po.id), "po_number": po_number, "amount_net": payload.amount_net, "amount_gross": amount_gross, "status": "draft"}

@router.get("/{po_id}")
async def get_po(po_id: str, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(PurchaseOrder).where(PurchaseOrder.id == po_id))
    po = r.scalar_one_or_none()
    if not po: raise HTTPException(404, "PO not found")
    items = (await db.execute(select(POItem).where(POItem.purchase_order_id == po_id).order_by(POItem.position_nr))).scalars().all()
    supplier = None
    if po.supplier_id:
        s = (await db.execute(select(Supplier).where(Supplier.id == po.supplier_id))).scalar_one_or_none()
        if s: supplier = {"id": str(s.id), "name": s.name, "contact_name": s.contact_name, "address": s.address, "country": s.country, "tax_id": s.tax_id}
    return {"id": str(po.id), "po_number": po.po_number, "status": po.status.value if po.status else "draft",
        "amount_net": po.amount_net, "amount_gross": po.amount_gross, "tax_rate": po.tax_rate,
        "orderer_name": po.orderer_name, "notes": po.notes, "supplier": supplier,
        "items": [{"position_nr": i.position_nr, "description": i.description, "quantity": i.quantity, "unit": i.unit, "unit_price": i.unit_price, "total_price": i.total_price} for i in items]}
