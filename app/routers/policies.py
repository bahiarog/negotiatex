"""
Phase 1 policy CRUD. Policies are authored by the tenant (simple CRUD) --
never written to by any agent/negotiation process.
"""
from typing import Any, Dict

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from deps import get_current_membership
from models_v2 import Policy

router = APIRouter()


class PolicyCreate(BaseModel):
    name: str
    rules_json: Dict[str, Any]
    is_active: bool = True


def _policy_to_dict(p: Policy) -> dict:
    return {
        "id": str(p.id), "name": p.name, "version": p.version, "is_active": p.is_active,
        "rules_json": p.rules_json, "created_at": p.created_at,
    }


@router.get("/policies")
async def list_policies(membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)):
    r = await db.execute(
        select(Policy).where(Policy.tenant_id == membership.tenant_id).order_by(Policy.created_at.desc())
    )
    return [_policy_to_dict(p) for p in r.scalars().all()]


@router.post("/policies")
async def create_policy(
    payload: PolicyCreate, membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db)
):
    # New version under the same name; deactivate prior versions of that name if this one is active.
    r = await db.execute(
        select(Policy)
        .where(Policy.tenant_id == membership.tenant_id, Policy.name == payload.name)
        .order_by(Policy.version.desc())
    )
    existing_versions = r.scalars().all()
    next_version = (existing_versions[0].version + 1) if existing_versions else 1

    if payload.is_active:
        for old in existing_versions:
            old.is_active = False

    policy = Policy(
        tenant_id=membership.tenant_id, name=payload.name, version=next_version,
        is_active=payload.is_active, rules_json=payload.rules_json,
    )
    db.add(policy)
    await db.commit()
    await db.refresh(policy)
    return _policy_to_dict(policy)
