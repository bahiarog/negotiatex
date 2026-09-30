from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from database import get_db
from models import Client, Audit, Offer, PurchaseOrder, ActivityLog

router = APIRouter()

@router.get("/dashboard")
async def dashboard(db: AsyncSession = Depends(get_db)):
    return {"totals": {
        "clients": (await db.execute(select(func.count(Client.id)))).scalar(),
        "audits": (await db.execute(select(func.count(Audit.id)))).scalar(),
        "offers": (await db.execute(select(func.count(Offer.id)))).scalar(),
        "purchase_orders": (await db.execute(select(func.count(PurchaseOrder.id)))).scalar(),
    }}

@router.get("/clients")
async def list_clients(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Client).order_by(Client.created_at.desc()))
    return [{"id": str(c.id), "company_name": c.company_name, "email": c.email, "industry": c.industry, "annual_volume": c.annual_volume, "created_at": c.created_at.isoformat() if c.created_at else None} for c in result.scalars().all()]

@router.get("/activity")
async def activity(limit: int = 50, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ActivityLog).order_by(ActivityLog.created_at.desc()).limit(limit))
    return [{"id": str(l.id), "entity_type": l.entity_type, "action": l.action, "message": l.message, "user_name": l.user_name, "created_at": l.created_at.isoformat() if l.created_at else None} for l in result.scalars().all()]


@router.get("/users")
async def list_users(db: AsyncSession = Depends(get_db)):
    """All registered users."""
    from routers.auth import User
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    users = result.scalars().all()
    return [
        {
            "id": str(u.id),
            "name": u.name,
            "email": u.email,
            "company_name": u.company_name,
            "is_admin": u.is_admin,
            "is_active": u.is_active,
            "plan": u.plan,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }
        for u in users
    ]


@router.patch("/users/{user_id}/plan")
async def update_plan(user_id: str, plan: str, db: AsyncSession = Depends(get_db)):
    from routers.auth import User
    from sqlalchemy import update
    valid = ["free", "paid", "trial"]
    if plan not in valid:
        from fastapi import HTTPException
        raise HTTPException(400, f"Invalid plan. Must be one of: {valid}")
    await db.execute(update(User).where(User.id == user_id).values(plan=plan))
    await db.commit()
    return {"user_id": user_id, "plan": plan}
