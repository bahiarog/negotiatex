"""
Zeitstrahl und Kundenbenachrichtigung fuer Vorhaben.

Andere Module (Outreach-Antworten, Angebotseingang, Verhandlung) melden
Ereignisse ueber event_for_request(); gehoert der Suchauftrag zu keinem
Vorhaben, passiert nichts. Meilensteine gehen zusaetzlich per E-Mail an den
Kunden -- kurz, mit Link auf die Statusseite, ohne vertrauliche Details
(Preise anderer Bieter erscheinen nie in einer Mail).
"""
import asyncio
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models_projects import Project, ProjectEvent

logger = logging.getLogger(__name__)

APP_URL = "https://negotiatex.ai"


async def project_for_request(db: AsyncSession, sourcing_request_id) -> Optional[Project]:
    if not sourcing_request_id:
        return None
    return (await db.execute(select(Project).where(Project.sourcing_request_id == sourcing_request_id))).scalars().first()


async def add_event(db: AsyncSession, project: Project, kind: str, title: str, detail: Optional[str] = None,
                    milestone: bool = False, actor: str = "agent", customer_visible: bool = True) -> ProjectEvent:
    ev = ProjectEvent(tenant_id=project.tenant_id, project_id=project.id, kind=kind, title=title[:255],
                      detail=detail, actor=str(actor), milestone=milestone, customer_visible=customer_visible)
    db.add(ev)
    await db.flush()
    if milestone and project.notify_milestones and project.customer_email:
        from services.email_sender import send_negotiation_email
        body = (
            f"Guten Tag,\n\nes gibt Neuigkeiten zu Ihrem Vorhaben \"{project.title}\":\n\n{title}\n"
            + (f"\n{detail}\n" if detail else "")
            + f"\nAktueller Stand und naechste Schritte:\n{APP_URL}/vorhaben/{project.id}\n\n"
            "Ihr NegotiateX-Agent\n(Automatische Statusmeldung -- bitte nicht auf diese E-Mail antworten.)"
        )
        try:
            res = await asyncio.to_thread(send_negotiation_email, project.customer_email,
                                          f"[NegotiateX] {project.title}: {title}"[:200], body)
            if res.get("sent"):
                ev.emailed_at = datetime.utcnow()
        except Exception:
            logger.exception(f"Meilenstein-Mail fuer Vorhaben {project.id} fehlgeschlagen")
    return ev


async def event_for_request(db: AsyncSession, sourcing_request_id, kind: str, title: str,
                            detail: Optional[str] = None, milestone: bool = False, actor: str = "agent") -> Optional[ProjectEvent]:
    project = await project_for_request(db, sourcing_request_id)
    if not project:
        return None
    return await add_event(db, project, kind, title, detail, milestone, actor)
