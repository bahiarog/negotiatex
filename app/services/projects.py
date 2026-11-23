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
                    milestone: bool = False, actor: str = "agent", customer_visible: bool = True,
                    link_tab: Optional[str] = None) -> ProjectEvent:
    ev = ProjectEvent(tenant_id=project.tenant_id, project_id=project.id, kind=kind, title=title[:255],
                      detail=detail, actor=str(actor), milestone=milestone, customer_visible=customer_visible)
    db.add(ev)
    await db.flush()
    if milestone and project.notify_milestones and project.customer_email:
        from services.email_sender import send_negotiation_email
        body = (
            f"Guten Tag,\n\nes gibt Neuigkeiten zu Ihrem Vorhaben \"{project.title}\":\n\n{title}\n"
            + (f"\n{detail}\n" if detail else "")
            + f"\nAktueller Stand und naechste Schritte:\n{APP_URL}/vorhaben/{project.id}"
            + (f"/{link_tab}" if link_tab else "") + "\n\n"
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


async def notify_team(db: AsyncSession, project: Project, title: str, detail: Optional[str] = None,
                      exclude: tuple = ()) -> int:
    """Procurement-Experten und Admins des Mandanten per E-Mail informieren
    (Auftrag erteilt, Entwuerfe warten auf Freigabe). Der Kunde bekommt seine
    Meilenstein-Mail separat; wer die Aktion selbst ausgeloest hat, wird
    uebersprungen. Rueckgabe: Anzahl versendeter Mails."""
    from access import roles_of
    from models_v2 import Membership
    from routers.auth import User
    from services.email_sender import send_negotiation_email
    rows = (await db.execute(select(Membership, User).join(User, User.id == Membership.user_id)
                             .where(Membership.tenant_id == project.tenant_id))).all()
    skip = {(e or "").lower() for e in (project.customer_email, *exclude)}
    recipients = sorted({u.email.lower() for m, u in rows
                         if u.email and roles_of(m) & {"procurement", "admin"} and u.email.lower() not in skip})
    body = (
        f"Guten Tag,\n\nim Vorhaben \"{project.title}\" gibt es Arbeit fuer das Einkaufsteam:\n\n{title}\n"
        + (f"\n{detail}\n" if detail else "")
        + f"\nZur Bearbeitung:\n{APP_URL}/arbeitsbereich/vorhaben/{project.id}/bearbeitung\n\n"
        "Ihr NegotiateX-Agent\n(Automatische Benachrichtigung fuer den Arbeitsbereich.)"
    )
    sent = 0
    for to in recipients:
        try:
            res = await asyncio.to_thread(send_negotiation_email, to, f"[NegotiateX] {project.title}: {title}"[:200], body)
            sent += bool(res.get("sent"))
        except Exception:
            logger.exception(f"Team-Mail fuer Vorhaben {project.id} an {to} fehlgeschlagen")
    return sent


async def event_for_request(db: AsyncSession, sourcing_request_id, kind: str, title: str,
                            detail: Optional[str] = None, milestone: bool = False, actor: str = "agent") -> Optional[ProjectEvent]:
    project = await project_for_request(db, sourcing_request_id)
    if not project:
        return None
    return await add_event(db, project, kind, title, detail, milestone, actor)
