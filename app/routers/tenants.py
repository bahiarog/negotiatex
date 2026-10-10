"""
Minimal tenant bootstrap -- needed because without it no logged-in user could
ever acquire a `memberships` row and every other Phase 1 endpoint 403s by
design. This is plain data-model plumbing (create a tenant, become its
owner), not part of the negotiation/approval pipeline, so it stays in scope
for Phase 1.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from deps import get_current_user
from models_v2 import Tenant, Membership, MembershipRole

router = APIRouter()


class TenantCreate(BaseModel):
    company_name: str


@router.post("/tenants")
async def create_tenant(payload: TenantCreate, user=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(Membership).where(Membership.user_id == user.id))
    if existing.scalar_one_or_none():
        raise HTTPException(400, "Benutzer ist bereits einem Mandanten zugeordnet.")

    tenant = Tenant(company_name=payload.company_name)
    db.add(tenant)
    await db.flush()
    # `memberships`' RLS policy is keyed on app.user_id (set in get_current_user),
    # not app.tenant_id -- this INSERT is a self-row-creation and needs no
    # tenant context to already exist, unlike every other tenant-scoped table.
    # Wer ein Unternehmenskonto anlegt, ist dessen Auftraggeber (entscheidungsbefugt)
    # und verwaltet es; den Arbeitsbereich vergibt ein Admin bei Bedarf gesondert.
    membership = Membership(tenant_id=tenant.id, user_id=user.id, role=MembershipRole.owner,
                            roles_json=["admin", "customer"], can_decide=True)
    db.add(membership)
    await db.commit()
    await db.refresh(tenant)
    return {"tenant_id": str(tenant.id), "company_name": tenant.company_name, "role": "owner"}


@router.get("/tenants/me")
async def my_tenant(user=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Membership).where(Membership.user_id == user.id))
    membership = r.scalar_one_or_none()
    if not membership:
        raise HTTPException(403, "Kein Zugriff: Benutzer ist keinem Mandanten zugeordnet.")
    r = await db.execute(select(Tenant).where(Tenant.id == membership.tenant_id))
    tenant = r.scalar_one_or_none()
    return {"tenant_id": str(membership.tenant_id), "company_name": tenant.company_name if tenant else None,
            "role": membership.role.value if hasattr(membership.role, "value") else membership.role}
