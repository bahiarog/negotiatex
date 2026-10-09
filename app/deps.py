"""
Phase 1 auth/tenant resolution helpers shared by the new `cases`/`policies`
routers.

Tenant isolation rule: the authenticated user's tenant_id is ALWAYS derived
from their `memberships` row server-side. It is never accepted from a
request body/query param. A user with no membership gets 403, not a
silently-assigned default tenant.

Teil C hardening: this is no longer the ONLY enforcement layer. Postgres
Row-Level Security policies (see rls_migration.sql) independently restrict
every tenant-scoped table to `tenant_id = current_setting('app.tenant_id')`,
and the app's own DB connection now runs as the unprivileged `negotiatex_app`
role (NOSUPERUSER NOBYPASSRLS) specifically so those policies cannot be
silently bypassed. `get_current_membership` sets that session variable via
`set_config(..., true)` (the `true` = is_local, same semantics as SET LOCAL)
as the FIRST statement in the request's transaction -- it is local to that
transaction, so it can never leak across pooled connections into a later
request for a different tenant. If this resolution is ever skipped for some
future endpoint, RLS still blocks cross-tenant reads/writes at the database
level; this app-layer check should not be loosened because of that.
"""
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from routers.auth import User, verify_token
from models_v2 import Membership


async def get_current_user(authorization: str = Header(default=None), db: AsyncSession = Depends(get_db)) -> User:
    if not authorization:
        raise HTTPException(status_code=401, detail="Fehlender Authorization-Header.")
    payload = verify_token(authorization)
    r = await db.execute(select(User).where(User.email == payload.get("email")))
    user = r.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Ungueltiger oder deaktivierter Benutzer.")
    # `memberships` is the one table whose own RLS policy cannot be keyed on
    # tenant_id (that's precisely the value this table's lookup exists to
    # discover -- see get_current_membership below). It is keyed on
    # app.user_id instead, set here, before that lookup ever runs.
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
    # is_local=false (SESSION-scoped, not transaction-scoped): several routers
    # call db.commit() partway through a request, which would end a LOCAL
    # setting's transaction early. Session-scope is safe here specifically
    # because this dependency runs FIRST on every protected request (before
    # any route handler code), so it always overwrites whatever value a
    # reused pooled connection happened to be carrying before this request
    # could read it -- a stale value is never observable by a handler.
    await db.execute(text("SELECT set_config('app.tenant_id', :tid, false)"), {"tid": str(membership.tenant_id)})
    return membership
