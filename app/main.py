from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from database import engine, admin_engine, Base
import models_v2  # noqa: F401 -- registers Phase 1 tables (tenants, cases, documents, ...) on Base.metadata
import models_requisitions  # noqa: F401 -- registers requisition/approval tables on Base.metadata
import models_invoices  # noqa: F401 -- registers invoice/line-item tables on Base.metadata
import models_negotiation  # noqa: F401 -- registers Teil A negotiation tables on Base.metadata
import models_sourcing  # noqa: F401 -- registers Teil B sourcing/outreach/NDA tables on Base.metadata
import models_contracts  # noqa: F401 -- registers Teil B7-B9 RFQ/offer/contract tables on Base.metadata
import models_mdc  # noqa: F401 -- registers Master Data Center (Etappe 1) tables on Base.metadata
from routers import audit, purchase_orders, admin, export, auth, benchmark, ws, apikeys, webhooks, public, cases, policies, tenants, suppliers, chat, requisitions, invoices, negotiation, sourcing, agent_overview, mdc
from routers.rfq_contracts import rfq_router, contracts_router

limiter = Limiter(key_func=get_remote_address)

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with admin_engine.begin() as conn:  # Teil C: create_all braucht CREATE-Rechte, die negotiatex_app bewusst nicht hat
        await conn.run_sync(Base.metadata.create_all)
    from services.autonomous_agent import start_scheduler
    scheduler = start_scheduler()
    from services.email_poller import register_negotiation_jobs
    register_negotiation_jobs(scheduler)
    yield
    scheduler.shutdown(wait=False)

app = FastAPI(title="NegotiateX.ai API", version="3.0.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

from prometheus_fastapi_instrumentator import Instrumentator
Instrumentator().instrument(app).expose(app, endpoint="/api/metrics", include_in_schema=False)
app.include_router(benchmark.router, prefix="/api/benchmark", tags=["Benchmark"])
app.include_router(auth.router, prefix="/api/auth", tags=["Auth"])
app.include_router(audit.router, prefix="/api/audit", tags=["Audit"])
app.include_router(purchase_orders.router, prefix="/api/po", tags=["Purchase Orders"])
app.include_router(suppliers.router, prefix="/api/suppliers", tags=["Suppliers"])
app.include_router(chat.router, prefix="/api/chat", tags=["Public Chat"])
app.include_router(admin.router, prefix="/api/admin", tags=["Admin"])
app.include_router(export.router, prefix="/api/export", tags=["Export"])
app.include_router(ws.router, prefix="/api", tags=["WebSocket"])
app.include_router(apikeys.router, prefix="/api", tags=["API Keys"])

app.include_router(webhooks.router, prefix="/api/webhooks", tags=["Webhooks"])
app.include_router(public.router, prefix="/api/public", tags=["Supplier Portal"])
app.include_router(requisitions.router, prefix="/api/requisitions", tags=["Requisitions"])
app.include_router(invoices.router, prefix="/api/invoices", tags=["Invoices"])

# Phase 1 (CTO briefing 7.10.2026): data model, upload, extraction, policy
# engine, comparisons, read-only dashboard. Mounted under /api/v1 so the
# existing nginx `location /api/` proxy on negotiatex.ai already covers it
# without any nginx changes.
app.include_router(tenants.router, prefix="/api/v1", tags=["Phase 1 - Tenants"])
app.include_router(cases.router, prefix="/api/v1", tags=["Phase 1 - Cases"])
app.include_router(policies.router, prefix="/api/v1", tags=["Phase 1 - Policies"])
app.include_router(negotiation.router, prefix="/api/v1/negotiation", tags=["Teil A - Negotiation"])
app.include_router(sourcing.router, prefix="/api/v1/sourcing", tags=["Teil B - Sourcing"])
app.include_router(rfq_router, prefix="/api/v1/rfq", tags=["Teil B7-B8 - RFQ & Angebote"])
app.include_router(contracts_router, prefix="/api/v1/contracts", tags=["Teil B9 - Vertraege"])
app.include_router(agent_overview.router, prefix="/api/v1/agent", tags=["Agent-Uebersicht (Freigaben & Aktivitaet)"])
app.include_router(mdc.router, prefix="/api/v1/mdc", tags=["Master Data Center (Etappe 1)"])

@app.get("/health")
async def health_root():
    return {"status": "ok", "service": "NegotiateX.ai", "version": "3.0.0"}

@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "NegotiateX.ai", "version": "3.0.0"}
