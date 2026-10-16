"""
Digitales Team (Arbeitsbereich): Profile, Status und Arbeitsprotokoll der
Agenten. Reiner Lesebetrieb -- nichts hier loest eine Aktion aus.

Status je Agent:
  working  -- ein Arbeitsschritt laeuft gerade (juenger als 15 Minuten)
  waiting  -- vorbereitete Ergebnisse warten auf eine menschliche Freigabe
  idle     -- nichts offen
Tokens und Kosten sehen nur Admins des Mandanten. Kosten werden nur
berechnet, wenn Preise je Million Tokens konfiguriert sind
(NX_PRICE_INPUT_PER_MTOK_EUR / NX_PRICE_OUTPUT_PER_MTOK_EUR) -- sonst None.
"""
import os
from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from access import Access, get_access, require_procurement
from database import get_db
from models_agents import AgentRun
from models_contracts import RFQ, RFQAction, RFQActionStatus, RFQOffer, OfferStatus
from models_mdc import MDCLineItem, MDCReviewStatus
from models_negotiation import NegotiationAction, NegotiationActionStatus, NegotiationException
from models_projects import Project, ProjectStatus
from models_sourcing import NDA, NDAStatus, OutreachAction, OutreachActionStatus, SupplierCandidate
from services.agent_log import AGENTS

router = APIRouter()


def _price(name):
    try:
        v = os.getenv(name)
        return Decimal(v) if v else None
    except Exception:
        return None


def _cost(inp: int, out: int):
    pi, po = _price("NX_PRICE_INPUT_PER_MTOK_EUR"), _price("NX_PRICE_OUTPUT_PER_MTOK_EUR")
    if pi is None or po is None:
        return None
    return str((Decimal(inp) * pi / Decimal(1_000_000) + Decimal(out) * po / Decimal(1_000_000)).quantize(Decimal("0.01")))


async def _count(db, q) -> int:
    return int((await db.execute(q)).scalar() or 0)


async def _waiting(db: AsyncSession, tid) -> dict:
    """Was je Agent vorbereitet ist und auf Menschen wartet."""
    active = select(Project.id).where(Project.tenant_id == tid, Project.archived_at.is_(None),
                                      Project.status.notin_([ProjectStatus.awarded, ProjectStatus.cancelled]))
    return {
        "briefing": (await _count(db, select(func.count()).select_from(Project).where(
            Project.tenant_id == tid, Project.archived_at.is_(None), Project.status == ProjectStatus.briefing)),
            "Entwürfe vor Auftragserteilung"),
        "scout": (await _count(db, select(func.count()).select_from(Project).where(
            Project.tenant_id == tid, Project.status == ProjectStatus.sourcing, Project.archived_at.is_(None),
            ~select(SupplierCandidate.id).where(SupplierCandidate.sourcing_request_id == Project.sourcing_request_id).exists())),
            "Vorhaben ohne Dienstleister-Auswahl"),
        "korrespondenz": (await _count(db, select(func.count()).select_from(OutreachAction).where(
            OutreachAction.tenant_id == tid, OutreachAction.status == OutreachActionStatus.draft)) +
            await _count(db, select(func.count()).select_from(RFQAction).where(
                RFQAction.tenant_id == tid, RFQAction.status == RFQActionStatus.draft)),
            "Nachrichten zur Freigabe"),
        "pruefer": (await _count(db, select(func.count()).select_from(RFQOffer).join(RFQ, RFQ.id == RFQOffer.rfq_id).join(
            Project, Project.sourcing_request_id == RFQ.sourcing_request_id).where(
            RFQOffer.tenant_id == tid, RFQOffer.status == OfferStatus.submitted, Project.id.in_(active),
            Project.awarded_offer_id.is_(None))),
            "Geprüfte Angebote zur Entscheidung"),
        "verhandler": (await _count(db, select(func.count()).select_from(NegotiationAction).where(
            NegotiationAction.tenant_id == tid, NegotiationAction.status == NegotiationActionStatus.draft)) +
            await _count(db, select(func.count()).select_from(NegotiationException).where(
                NegotiationException.tenant_id == tid, NegotiationException.resolved == False)),  # noqa: E712
            "Preisvorschläge und Eskalationen"),
        "recht": (await _count(db, select(func.count()).select_from(NDA).where(
            NDA.tenant_id == tid, NDA.status.in_([NDAStatus.draft, NDAStatus.returned, NDAStatus.review_required, NDAStatus.verified]))),
            "Vertraulichkeitsvereinbarungen in Prüfung"),
        "analyst": (await _count(db, select(func.count()).select_from(MDCLineItem).where(
            MDCLineItem.tenant_id == tid, MDCLineItem.review_status.in_([MDCReviewStatus.extracted, MDCReviewStatus.needs_review]))),
            "Preispositionen zur Prüfung im Data Center"),
    }


async def _titles(db, runs) -> dict:
    ids = {r.project_id for r in runs if r.project_id}
    if not ids:
        return {}
    return {p.id: p.title for p in (await db.execute(select(Project).where(Project.id.in_(ids)))).scalars().all()}


def _run_dict(r: AgentRun, titles: dict, admin: bool) -> dict:
    d = {"id": str(r.id), "agent": r.agent_key, "task": r.task, "status": r.status, "outcome": r.outcome,
         "project_id": str(r.project_id) if r.project_id else None, "project_title": titles.get(r.project_id),
         "object_ref": r.object_ref, "needs_approval": r.needs_approval,
         "started_at": r.started_at.isoformat() if r.started_at else None,
         "finished_at": r.finished_at.isoformat() if r.finished_at else None, "duration_ms": r.duration_ms,
         "llm_calls": r.llm_calls}
    if admin:
        d.update({"input_tokens": r.input_tokens, "output_tokens": r.output_tokens, "cost_eur": _cost(r.input_tokens, r.output_tokens)})
    return d


@router.get("")
async def team(access: Access = Depends(require_procurement), db: AsyncSession = Depends(get_db)):
    tid = access.tenant_id
    now = datetime.utcnow()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week0 = day0 - timedelta(days=6)
    waiting = await _waiting(db, tid)
    runs = (await db.execute(select(AgentRun).where(AgentRun.tenant_id == tid, AgentRun.started_at >= week0)
                             .order_by(AgentRun.started_at.desc()))).scalars().all()
    titles = await _titles(db, runs[:200])
    out = []
    for a in AGENTS:
        mine = [r for r in runs if r.agent_key == a["key"]]
        running = [r for r in mine if r.status == "running" and r.started_at and r.started_at > now - timedelta(minutes=15)]
        today = [r for r in mine if r.started_at and r.started_at >= day0 and r.status != "running"]
        per_day = []
        for i in range(7):
            d0 = week0 + timedelta(days=i)
            per_day.append({"date": d0.date().isoformat(),
                            "count": sum(1 for r in mine if r.started_at and d0 <= r.started_at < d0 + timedelta(days=1))})
        w_count, w_label = waiting.get(a["key"], (0, ""))
        status = "working" if running else ("waiting" if w_count else "idle")
        durs = [r.duration_ms for r in today if r.duration_ms]
        item = {
            **a, "status": status,
            "current": [_run_dict(r, titles, access.admin) for r in running],
            "waiting": {"count": w_count, "label": w_label},
            "today": {"done": sum(1 for r in today if r.status == "done"), "failed": sum(1 for r in today if r.status == "failed"),
                      "avg_seconds": round(sum(durs) / len(durs) / 1000, 1) if durs else None},
            "week": per_day,
            "recent": [_run_dict(r, titles, access.admin) for r in mine[:6]],
            "last_active": mine[0].started_at.isoformat() if mine else None,
        }
        if access.admin:
            inp, outp = sum(r.input_tokens for r in today), sum(r.output_tokens for r in today)
            item["today"].update({"input_tokens": inp, "output_tokens": outp, "cost_eur": _cost(inp, outp),
                                  "llm_calls": sum(r.llm_calls for r in today)})
        out.append(item)
    return {"agents": out, "generated_at": now.isoformat(), "costs_configured": _cost(0, 0) is not None,
            "show_costs": access.admin}


@router.get("/{agent_key}/runs")
async def runs(agent_key: str, limit: int = 50, access: Access = Depends(require_procurement), db: AsyncSession = Depends(get_db)):
    if agent_key not in {a["key"] for a in AGENTS}:
        raise HTTPException(404, "Unbekannter Agent.")
    rows = (await db.execute(select(AgentRun).where(AgentRun.tenant_id == access.tenant_id, AgentRun.agent_key == agent_key)
                             .order_by(AgentRun.started_at.desc()).limit(min(max(limit, 1), 200)))).scalars().all()
    titles = await _titles(db, rows)
    return [_run_dict(r, titles, access.admin) for r in rows]


@router.get("/profiles")
async def profiles(access: Access = Depends(get_access)):
    """Profile (Name, Rolle, Farbe, Symbol) fuer Avatare -- auch im Kundenbereich, ohne Arbeitsdaten."""
    return [{k: a[k] for k in ("key", "name", "role", "color", "icon", "skills")} for a in AGENTS]
