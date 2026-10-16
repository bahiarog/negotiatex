"""
Digitales Team -- Profile der Agenten und Protokollierung ihrer Arbeitsschritte.

Verwendung an einer Arbeitsstelle:

    async with track("pruefer", "Angebot pruefen", tenant_id=..., project_id=...) as run:
        ...
        run.outcome = "2 kritische Punkte gefunden"

- Geschrieben wird ueber eine eigene Admin-Session: das Protokoll beeinflusst
  die Transaktion der eigentlichen Arbeit nie, und ein Fehler im Protokoll
  bricht die Arbeit nie ab.
- KI-Tokens werden zentral erfasst (install_usage_hook): jeder Aufruf von
  anthropic Messages.create innerhalb eines `track`-Blocks wird diesem
  Arbeitsschritt zugerechnet -- auch in Threads (asyncio.to_thread kopiert
  den Kontext).
- annotate() ergaenzt Mandant/Vorhaben, wenn sie erst waehrend der Arbeit
  bekannt werden (z.B. bei eingehenden Mails).
"""
import contextvars
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# Profile -- jede Rolle entspricht einer bestehenden Funktion des Systems.
AGENTS = [
    {"key": "briefing", "name": "Briefing-Assistent", "color": "#2f6f8f", "icon": "clipboard",
     "role": "Macht aus Ihrer Beschreibung oder Ihrem Dokument ein strukturiertes Briefing.",
     "skills": ["Bedarf strukturieren", "Muss-Kriterien erkennen", "Fehlende Angaben als Fragen", "Nichts erfinden"]},
    {"key": "scout", "name": "Scout", "color": "#7a5c9e", "icon": "search",
     "role": "Findet passende Dienstleister im eigenen Bestand und sortiert sie nach Passung.",
     "skills": ["Lieferantenstamm durchsuchen", "Frühere Anfragen auswerten", "Semantische Passung", "Data Center einbeziehen"]},
    {"key": "korrespondenz", "name": "Korrespondenz", "color": "#b5762a", "icon": "mail",
     "role": "Bereitet Anfragen und Erinnerungen vor und ordnet eingehende Antworten zu.",
     "skills": ["Erstanfragen entwerfen", "Erinnerungen", "Antworten zuordnen und einordnen", "Unzustellbare Mails erkennen"]},
    {"key": "pruefer", "name": "Angebotsprüfer", "color": "#1f7a5c", "icon": "check",
     "role": "Liest Angebote aus, prüft Konditionen und AGB gegen Ihre Vorgaben.",
     "skills": ["Preise und Konditionen auslesen", "AGB-Risiken mit Zitat", "Vergleichbarkeit", "Budget- und Terminabgleich"]},
    {"key": "verhandler", "name": "Verhandler", "color": "#a3473a", "icon": "handshake",
     "role": "Formuliert Preisvorschläge im bestätigten Rahmen und wertet Gegenangebote aus.",
     "skills": ["Zwei Preisrunden", "Nie über der Obergrenze", "Gegenangebote erkennen", "Ausnahmen eskalieren"]},
    {"key": "recht", "name": "Rechtsassistenz", "color": "#4a5a73", "icon": "scale",
     "role": "Schätzt ein, ob eine Vertraulichkeitsvereinbarung nötig ist, und findet Abweichungen.",
     "skills": ["NDA-Bedarf einschätzen", "Abweichungen erkennen", "Unterschriftsangaben markieren", "An Experten übergeben"]},
    {"key": "analyst", "name": "Data-Center-Analyst", "color": "#56743a", "icon": "chart",
     "role": "Liest Ratecards und Preislisten aus und liefert Preisreferenzen.",
     "skills": ["Preispositionen auslesen", "Normalisieren und prüfen", "Marktvergleich", "Semantische Suche"]},
]
AGENT_KEYS = {a["key"] for a in AGENTS}

# Welche Vorhaben-Ereignisse stammen von welchem Agenten (fuer Kundenverlauf)
EVENT_AGENT = {
    "offer_received": "pruefer", "delivery_failed": "korrespondenz", "supplier_message": "korrespondenz",
    "reply_interest": "korrespondenz", "reply_decline": "korrespondenz", "reply_question": "korrespondenz",
    "reply_no_signal": "korrespondenz", "negotiation_reply": "verhandler", "nda_approved": "recht",
}

_current: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("agent_run", default=None)


class _Run:
    def __init__(self, data: dict):
        self._d = data

    @property
    def outcome(self):
        return self._d.get("outcome")

    @outcome.setter
    def outcome(self, v):
        self._d["outcome"] = (str(v) if v is not None else None)

    def add_pending(self, n: int = 1):
        self._d["needs_approval"] = self._d.get("needs_approval", 0) + n


def annotate(tenant_id=None, project_id=None, object_ref=None, task=None, outcome=None, needs_approval=None, agent_key=None):
    d = _current.get()
    if d is None:
        return
    if agent_key in AGENT_KEYS:
        d["agent_key"] = agent_key
    if tenant_id is not None:
        d["tenant_id"] = tenant_id
    if project_id is not None:
        d["project_id"] = project_id
    if object_ref is not None:
        d["object_ref"] = str(object_ref)[:255]
    if task:
        d["task"] = task[:255]
    if outcome is not None:
        d["outcome"] = str(outcome)
    if needs_approval:
        d["needs_approval"] = d.get("needs_approval", 0) + int(needs_approval)


async def _write(fn):
    try:
        from database import AdminSessionLocal
        async with AdminSessionLocal() as s:
            await fn(s)
            await s.commit()
    except Exception:
        logger.exception("Agenten-Protokoll konnte nicht geschrieben werden")


@asynccontextmanager
async def track(agent_key: str, task: str, tenant_id=None, project_id=None, object_ref=None):
    from models_agents import AgentRun
    d = {"id": uuid.uuid4(), "agent_key": agent_key, "task": task[:255], "tenant_id": tenant_id,
         "project_id": project_id, "object_ref": str(object_ref)[:255] if object_ref else None,
         "outcome": None, "needs_approval": 0, "llm_calls": 0, "input_tokens": 0, "output_tokens": 0}
    t0 = time.monotonic()
    token = _current.set(d)

    async def start(s):
        s.add(AgentRun(id=d["id"], tenant_id=d["tenant_id"], agent_key=agent_key, task=d["task"],
                       project_id=d["project_id"], object_ref=d["object_ref"], status="running"))
    await _write(start)
    status = "done"
    try:
        yield _Run(d)
    except Exception as e:
        status = "failed"
        d["outcome"] = d.get("outcome") or f"Fehler: {type(e).__name__}"
        raise
    finally:
        _current.reset(token)

        async def finish(s):
            from sqlalchemy import update
            await s.execute(update(AgentRun).where(AgentRun.id == d["id"]).values(
                status=status, tenant_id=d["tenant_id"], project_id=d["project_id"], object_ref=d["object_ref"],
                agent_key=d["agent_key"], task=d["task"], outcome=(d["outcome"] or None) and d["outcome"][:2000], needs_approval=d["needs_approval"],
                llm_calls=d["llm_calls"], input_tokens=d["input_tokens"], output_tokens=d["output_tokens"],
                finished_at=datetime.utcnow(), duration_ms=int((time.monotonic() - t0) * 1000)))
        await _write(finish)


def install_usage_hook():
    """Rechnet Tokens jedes KI-Aufrufs dem laufenden Arbeitsschritt zu."""
    try:
        from anthropic.resources.messages import Messages
    except Exception:
        logger.warning("Agenten-Protokoll: anthropic Messages nicht gefunden -- Tokens werden nicht erfasst.")
        return
    if getattr(Messages.create, "_nx_wrapped", False):
        return
    original = Messages.create

    def create(self, *args, **kwargs):
        resp = original(self, *args, **kwargs)
        d = _current.get()
        if d is not None:
            try:
                u = getattr(resp, "usage", None)
                d["llm_calls"] += 1
                d["input_tokens"] += int(getattr(u, "input_tokens", 0) or 0)
                d["output_tokens"] += int(getattr(u, "output_tokens", 0) or 0)
            except Exception:
                pass
        return resp
    create._nx_wrapped = True
    Messages.create = create


async def mark_stale_runs():
    """Schritte, die nach einem Neustart als 'running' haengen geblieben sind, abschliessen."""
    async def fix(s):
        from sqlalchemy import text
        await s.execute(text("UPDATE agent_runs SET status='failed', outcome=COALESCE(outcome,'Abgebrochen (Neustart)'), "
                             "finished_at=now() WHERE status='running' AND started_at < now() - interval '15 minutes'"))
    await _write(fix)
