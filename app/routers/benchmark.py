"""
NegotiateX — Benchmark Router
Manages the benchmark data pool for market comparisons.
"""
import uuid
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_
from pydantic import BaseModel
from database import get_db
from models import BenchmarkEntry, Offer

router = APIRouter()

METRIC_LABELS = {
    "hourly_rate": "Stundensatz",
    "daily_rate": "Tagessatz",
    "fte": "FTE-Kosten/Monat",
    "flat": "Pauschalpreis",
    "monthly": "Monatspauschale",
    "cpl": "Cost per Lead",
}

class BenchmarkCreate(BaseModel):
    category: str
    metric_type: str
    metric_label: Optional[str] = None
    value: float
    currency: str = "EUR"
    region: str = "DACH"
    source: str = "manual"
    offer_id: Optional[str] = None
    audit_id: Optional[str] = None
    supplier_anonymized: Optional[str] = None
    notes: Optional[str] = None

@router.post("/entries")
async def create_entry(payload: BenchmarkCreate, db: AsyncSession = Depends(get_db)):
    entry = BenchmarkEntry(
        category=payload.category, metric_type=payload.metric_type,
        metric_label=payload.metric_label, value=payload.value,
        currency=payload.currency, region=payload.region,
        source=payload.source, offer_id=payload.offer_id,
        audit_id=payload.audit_id,
        supplier_anonymized=payload.supplier_anonymized,
        notes=payload.notes,
    )
    db.add(entry); await db.commit(); await db.refresh(entry)
    return {"id": str(entry.id), "value": entry.value, "category": entry.category}

@router.get("/entries")
async def list_entries(
    category: Optional[str] = None,
    metric_type: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    q = select(BenchmarkEntry).order_by(BenchmarkEntry.created_at.desc())
    if category: q = q.where(BenchmarkEntry.category == category)
    if metric_type: q = q.where(BenchmarkEntry.metric_type == metric_type)
    result = await db.execute(q)
    entries = result.scalars().all()
    return [{"id": str(e.id), "category": e.category, "metric_type": e.metric_type,
             "metric_label": e.metric_label, "value": e.value, "currency": e.currency,
             "region": e.region, "source": e.source, "verified": e.verified,
             "notes": e.notes, "created_at": e.created_at.isoformat() if e.created_at else None}
            for e in entries]

@router.get("/compare")
async def compare(
    category: str,
    metric_type: str,
    value: float,
    db: AsyncSession = Depends(get_db)
):
    """Compare a value against the benchmark pool."""
    result = await db.execute(
        select(BenchmarkEntry).where(
            and_(BenchmarkEntry.category == category,
                 BenchmarkEntry.metric_type == metric_type)
        )
    )
    entries = result.scalars().all()
    if not entries:
        return {"has_data": False, "count": 0}
    values = [e.value for e in entries]
    avg = sum(values) / len(values)
    mn = min(values); mx = max(values)
    sorted_v = sorted(values)
    p25 = sorted_v[max(0,len(sorted_v)//4)]
    p75 = sorted_v[min(len(sorted_v)-1,3*len(sorted_v)//4)]
    diff_pct = ((value - avg) / avg * 100) if avg else 0
    verdict = "fair" if abs(diff_pct) <= 10 else ("overpriced" if diff_pct > 0 else "below_market")
    return {
        "has_data": True, "count": len(values),
        "your_value": value, "avg": round(avg, 2),
        "min": round(mn, 2), "max": round(mx, 2),
        "p25": round(p25, 2), "p75": round(p75, 2),
        "diff_pct": round(diff_pct, 1),
        "verdict": verdict,
        "metric_label": METRIC_LABELS.get(metric_type, metric_type),
        "category": category,
    }

@router.get("/stats")
async def stats(db: AsyncSession = Depends(get_db)):
    """Dashboard stats for benchmark pool."""
    total = (await db.execute(select(func.count(BenchmarkEntry.id)))).scalar()
    verified = (await db.execute(select(func.count(BenchmarkEntry.id)).where(BenchmarkEntry.verified==True))).scalar()
    # Count per category
    cats = (await db.execute(
        select(BenchmarkEntry.category, func.count(BenchmarkEntry.id))
        .group_by(BenchmarkEntry.category)
        .order_by(func.count(BenchmarkEntry.id).desc())
    )).all()
    # Count per metric type
    metrics = (await db.execute(
        select(BenchmarkEntry.metric_type, func.avg(BenchmarkEntry.value), func.count(BenchmarkEntry.id))
        .group_by(BenchmarkEntry.metric_type)
    )).all()
    return {
        "total": total, "verified": verified,
        "categories": [{"name": c[0], "count": c[1]} for c in cats],
        "metrics": [{"type": m[0], "avg": round(float(m[1]),2) if m[1] else 0, "count": m[2]} for m in metrics],
    }

@router.patch("/entries/{entry_id}/verify")
async def verify_entry(entry_id: str, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(BenchmarkEntry).where(BenchmarkEntry.id == entry_id))
    e = r.scalar_one_or_none()
    if not e: raise HTTPException(404, "Not found")
    e.verified = True; await db.commit()
    return {"id": entry_id, "verified": True}

@router.delete("/entries/{entry_id}")
async def delete_entry(entry_id: str, db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(BenchmarkEntry).where(BenchmarkEntry.id == entry_id))
    e = r.scalar_one_or_none()
    if not e: raise HTTPException(404, "Not found")
    await db.delete(e); await db.commit()
    return {"deleted": True}
