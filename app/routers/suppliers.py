from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional
from database import get_db
from models import Supplier

router = APIRouter()


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
