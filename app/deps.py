"""
Phase 1 auth/tenant resolution helpers shared by the new `cases`/`policies`
routers.

Tenant isolation rule: the authenticated user's tenant_id is ALWAYS derived
from their `memberships` row server-side. It is never accepted from a
request body/query param. A user with no membership gets 403, not a
silently-assigned default tenant.

Teil C hardening: this is no longer the ONLY enforcement layer. Postgres
Row-Level Security policies (see migrations/rls_tenant_isolation.sql)
independently restrict every tenant-scoped table to
`tenant_id = current_setting('app.tenant_id')`, and the app's own DB
connection now runs as the unprivileged `negotiatex_app` role (NOSUPERUSER
NOBYPASSRLS) specifically so those policies cannot be silently bypassed.

Applied via two complementary mechanisms, both needed:
1. A direct `db.execute(SET ...)` right here, for the request's current,
   already-open transaction -- by the time we know the tenant_id (it comes
   from a membership row we just queried, inside that same transaction),
   SQLAlchemy's `after_begin` event has already fired for it, so nothing
   would apply the value to THIS transaction without this direct call.
2. `database.py`'s `after_begin` listener, reading the `current_tenant_id`/
   `current_user_id` context vars set below, for any LATER transaction the
   same request's session opens (several routers call `db.commit()`
   mid-request, and ORM Session commits check the DBAPI connection back
   into the pool -- the next statement can land on a different connection
   that never saw a direct SET at all).
Context vars are per-asyncio-Task, so concurrent requests never see each
other's values. If this resolution is ever skipped for some future
endpoint, RLS still blocks cross-tenant reads/writes at the database level;
this app-layer check should not be loosened because of that.
"""
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db, current_tenant_id, current_user_id
from routers.auth import User, verify_token
from models_v2 import Membership


async def get_current_user(authorization: str = Header(default=None), db: AsyncSession = Depends(get_db)) -> User:
    if not authorization:
        raise HTTPException(status_code=401, detail="Fehlender Authorization-Header.")
    payload = verify_token(authorization)
    # Set from the JWT's own "id" claim, before any DB query runs in this
    # request -- `memberships` (the table that would otherwise tell us the
    # user id) has its own RLS policy keyed on this exact value, so it must
    # already be set before that table is ever queried, not derived from it.
    # Applied twice deliberately: the context var is for database.py's
    # `after_begin` listener (covers any LATER transaction this request
    # opens, e.g. after a mid-request commit), and the direct SET below is
    # needed because `after_begin` already fired for THIS request's current,
    # still-open transaction before this value was knowable -- without this,
    # the very first transaction would never see it at all.
    if payload.get("id"):
        current_user_id.set(str(payload["id"]))
        await db.execute(text("SELECT set_config('app.user_id', :uid, false)"), {"uid": str(payload["id"])})
    r = await db.execute(select(User).where(User.email == payload.get("email")))
    user = r.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Ungueltiger oder deaktivierter Benutzer.")
    current_user_id.set(str(user.id))  # authoritative DB value, in case the token claim ever drifts
    await db.execute(text("SELECT set_config('app.user_id', :uid, false)"), {"uid": str(user.id)})
    return user


async def get_current_membership(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> Membership:
    r = await db.execute(select(Membership).where(Membership.user_id == user.id))
    membership = r.scalar_one_or_none()
    if not membership:
        raise HTTPException(
            status_code=403,
            detail="Kein Zugriff: Benutzer ist keinem Mandanten (Tenant) zugeordnet.",
        )
    current_tenant_id.set(str(membership.tenant_id))
    # Same reason as above: apply immediately to the already-open
    # transaction, not just to the context var for future transactions.
    await db.execute(text("SELECT set_config('app.tenant_id', :tid, false)"), {"tid": str(membership.tenant_id)})
    return membership
