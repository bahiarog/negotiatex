import os
import contextvars
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Session

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://negotiatex:negotiatex_pw@negotiatex-db:5432/negotiatex")
engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Background system processes (the IMAP poller, reminder-window checks) are
# a deliberate, narrow exception to RLS: they aren't serving a specific
# tenant's request, their whole job is to scan an inbound message across
# ALL tenants' outbound messages to find which one it's a reply to -- no
# single app.tenant_id value could ever make that query work. This is not a
# weakening of the per-request isolation guarantee tested in
# rls_tenant_isolation.sql; it's the standard pattern for a trusted,
# code-reviewed system worker vs. a request handler driven by arbitrary
# user input. ADMIN_DATABASE_URL is the original superuser connection
# (BYPASSRLS), kept specifically for this and for migrations/admin psql
# work -- it must never be used for anything reachable from a user request.
ADMIN_DATABASE_URL = os.getenv("ADMIN_DATABASE_URL", DATABASE_URL)
admin_engine = create_async_engine(ADMIN_DATABASE_URL, echo=False, pool_pre_ping=True)
AdminSessionLocal = async_sessionmaker(admin_engine, class_=AsyncSession, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

# RLS context (Teil C): a plain "SET ... session-scoped" executed once per
# request is not enough -- SQLAlchemy's ORM Session checks its DBAPI
# connection back into the pool on every commit(), and several routers
# commit more than once per request. The *next* statement after such a
# commit can land on a different pooled connection that never had
# app.tenant_id set on it, silently making every RLS-protected query see
# zero rows (fail-closed, but breaks the request, e.g. a post-commit
# db.refresh()). Fixing this per-router would mean touching every commit
# site. Instead: a context var set once in deps.py, applied to every new
# transaction automatically via "after_begin", which fires on every
# transaction -- including the implicit one SQLAlchemy opens right after a
# commit. Listening on the plain `Session` class (not `AsyncSession`) is the
# documented way to hook ORM events for an async session, since AsyncSession
# wraps a real sync Session internally and this event is only ever emitted
# on that sync side.
current_tenant_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_tenant_id", default=None)
current_user_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_user_id", default=None)


@event.listens_for(Session, "after_begin")
def _apply_rls_context(session, transaction, connection):
    tid = current_tenant_id.get()
    uid = current_user_id.get()
    if tid:
        connection.execute(text("SELECT set_config('app.tenant_id', :v, true)"), {"v": tid})
    if uid:
        connection.execute(text("SELECT set_config('app.user_id', :v, true)"), {"v": uid})


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
