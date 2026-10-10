"""
Zugang des angemeldeten Nutzers (Grundlage der Navigation) und
Mandanten-Administration: Nutzer, Rollen, Entscheidungsbefugnis, Einstellungen.

Rollen werden ausschliesslich hier vergeben und in access.py fuer jede Route
serverseitig geprueft. Admins koennen sich selbst nicht die letzte
Admin-Rolle entziehen (Aussperrschutz).
"""
import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from access import ROLE_LABELS, ROLES, Access, get_access, require_admin, require_workspace, roles_of
from database import get_db
from models_v2 import Membership, MembershipRole, Tenant
from routers.auth import PasswordResetToken, User, hash_password

logger = logging.getLogger(__name__)
router = APIRouter()


def _field(field, message, status=422):
    return HTTPException(status, {"field": field, "message": message})


@router.get("/me/access")
async def my_access(access: Access = Depends(get_access), db: AsyncSession = Depends(get_db)):
    tenant = (await db.execute(select(Tenant).where(Tenant.id == access.tenant_id))).scalar_one_or_none()
    return {
        "user": {"id": str(access.user.id), "name": access.user.name, "email": access.user.email},
        "tenant": {"id": str(access.tenant_id), "company_name": tenant.company_name if tenant else None},
        "roles": sorted(access.roles), "role_labels": [ROLE_LABELS[r] for r in ROLES if r in access.roles],
        "can_decide": access.can_decide, "can_approve_messages": access.can_approve_messages,
        "areas": {"customer": True, "workspace": access.workspace, "procurement": access.procurement, "admin": access.admin},
        "platform_admin": bool(getattr(access.user, "is_admin", False)),
    }


def _member_dict(m: Membership, u: User, me_id) -> dict:
    roles = roles_of(m)
    return {"user_id": str(u.id), "name": u.name, "email": u.email, "active": u.is_active,
            "roles": sorted(roles), "role_labels": [ROLE_LABELS[r] for r in ROLES if r in roles],
            "can_decide": bool(m.can_decide), "is_me": u.id == me_id,
            "since": m.created_at.isoformat() if m.created_at else None}


async def _members(db: AsyncSession, tenant_id):
    return (await db.execute(select(Membership, User).join(User, User.id == Membership.user_id)
                             .where(Membership.tenant_id == tenant_id).order_by(User.name))).all()


@router.get("/admin/members")
async def list_members(access: Access = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    return [_member_dict(m, u, access.user.id) for m, u in await _members(db, access.tenant_id)]


@router.get("/members/procurement")
async def procurement_members(access: Access = Depends(require_workspace), db: AsyncSession = Depends(get_db)):
    """Auswahl fuer 'Verantwortlicher' im Arbeitsbereich."""
    return [{"user_id": str(u.id), "name": u.name or u.email} for m, u in await _members(db, access.tenant_id)
            if "procurement" in roles_of(m)]


class MemberPayload(BaseModel):
    email: Optional[str] = None
    name: Optional[str] = None
    roles: list[str] = ["customer"]
    can_decide: bool = False


def _clean_roles(roles: list[str]) -> list[str]:
    out = sorted({r for r in roles if r in ROLES})
    if not out:
        raise _field("roles", "Bitte mindestens eine Rolle wählen.")
    return out


async def _admins_left(db, tenant_id, excluding_user_id) -> int:
    return sum(1 for m, u in await _members(db, tenant_id) if u.id != excluding_user_id and "admin" in roles_of(m))


@router.post("/admin/members")
async def invite_member(payload: MemberPayload, access: Access = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Nutzer einladen: legt das Konto an (falls noch nicht vorhanden) und
    schickt einen Link zum Setzen des Passworts (72 Stunden gueltig)."""
    email = (payload.email or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise _field("email", "Bitte eine gültige E-Mail-Adresse angeben.")
    roles = _clean_roles(payload.roles)
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user:
        other = (await db.execute(select(Membership).where(Membership.user_id == user.id))).scalar_one_or_none()
        if other:
            raise _field("email", "Diese Person ist bereits einem Mandanten zugeordnet.", 409)
    else:
        user = User(name=(payload.name or "").strip() or email.split("@")[0], email=email, company_name=None,
                    password_hash=hash_password(secrets.token_urlsafe(24)), is_admin=False, plan="free")
        db.add(user)
        await db.flush()
    db.add(Membership(tenant_id=access.tenant_id, user_id=user.id, role=MembershipRole.member,
                      roles_json=roles, can_decide=payload.can_decide))
    raw = secrets.token_urlsafe(32)
    db.add(PasswordResetToken(user_id=user.id, token_hash=hashlib.sha256(raw.encode()).hexdigest(),
                              expires_at=datetime.utcnow() + timedelta(hours=72)))
    tenant = (await db.execute(select(Tenant).where(Tenant.id == access.tenant_id))).scalar_one_or_none()
    await db.commit()
    sent = False
    try:
        from services.email_sender import send_negotiation_email
        body = (f"Guten Tag {user.name or ''},\n\n{access.user.name or access.user.email} hat Sie zu NegotiateX.ai eingeladen"
                f"{' (' + tenant.company_name + ')' if tenant and tenant.company_name else ''}.\n\n"
                f"Ihre Rolle(n): {', '.join(ROLE_LABELS[r] for r in roles)}"
                f"{' · mit Entscheidungsbefugnis' if payload.can_decide else ''}\n\n"
                f"Bitte legen Sie hier Ihr Passwort fest (Link 72 Stunden gültig):\n"
                f"https://negotiatex.ai/reset-password?token={raw}\n\n"
                "Anschließend melden Sie sich unter https://negotiatex.ai/login an.\n\nIhr NegotiateX-Team")
        sent = bool(send_negotiation_email(email, "Einladung zu NegotiateX.ai", body).get("sent"))
    except Exception:
        logger.exception("Einladungs-Mail fehlgeschlagen")
    return {"user_id": str(user.id), "email": email, "invitation_sent": sent}


@router.put("/admin/members/{user_id}")
async def update_member(user_id: str, payload: MemberPayload, access: Access = Depends(require_admin),
                        db: AsyncSession = Depends(get_db)):
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(404, "Nutzer nicht gefunden.")
    m = (await db.execute(select(Membership).where(Membership.user_id == uid, Membership.tenant_id == access.tenant_id))).scalar_one_or_none()
    if not m:
        raise HTTPException(404, "Nutzer nicht gefunden.")
    roles = _clean_roles(payload.roles)
    if "admin" not in roles and "admin" in roles_of(m) and await _admins_left(db, access.tenant_id, uid) == 0:
        raise _field("roles", "Mindestens eine Person muss Admin bleiben.")
    m.roles_json, m.can_decide = roles, payload.can_decide
    await db.commit()
    u = (await db.execute(select(User).where(User.id == uid))).scalar_one()
    return _member_dict(m, u, access.user.id)


@router.delete("/admin/members/{user_id}")
async def remove_member(user_id: str, access: Access = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(404, "Nutzer nicht gefunden.")
    if uid == access.user.id:
        raise HTTPException(400, "Sie können sich nicht selbst entfernen.")
    m = (await db.execute(select(Membership).where(Membership.user_id == uid, Membership.tenant_id == access.tenant_id))).scalar_one_or_none()
    if not m:
        raise HTTPException(404, "Nutzer nicht gefunden.")
    await db.delete(m)
    await db.commit()
    return {"removed": True}


class SettingsPayload(BaseModel):
    company_name: Optional[str] = None
    success_fee_pct: Optional[str] = None


@router.get("/admin/settings")
async def get_settings(access: Access = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    t = (await db.execute(select(Tenant).where(Tenant.id == access.tenant_id))).scalar_one()
    return {"company_name": t.company_name, "success_fee_pct": str(t.success_fee_pct) if t.success_fee_pct is not None else None}


@router.put("/admin/settings")
async def put_settings(payload: SettingsPayload, access: Access = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    t = (await db.execute(select(Tenant).where(Tenant.id == access.tenant_id))).scalar_one()
    if payload.company_name is not None:
        if not payload.company_name.strip():
            raise _field("company_name", "Bitte einen Firmennamen angeben.")
        t.company_name = payload.company_name.strip()[:255]
    if payload.success_fee_pct is not None:
        raw = payload.success_fee_pct.strip().replace(",", ".")
        if raw == "":
            t.success_fee_pct = None
        else:
            try:
                v = Decimal(raw)
            except InvalidOperation:
                raise _field("success_fee_pct", "Bitte eine Zahl zwischen 0 und 100 angeben.")
            if not (Decimal("0") <= v <= Decimal("100")):
                raise _field("success_fee_pct", "Bitte eine Zahl zwischen 0 und 100 angeben.")
            t.success_fee_pct = v
    await db.commit()
    return await get_settings(access, db)
