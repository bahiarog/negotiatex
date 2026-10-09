"""
APScheduler-Jobs fuer Teil A: (1) IMAP-Posteingang abfragen und Antworten
Faellen zuordnen, (2) "keine Antwort innerhalb des Fensters"-Pruefung (A5,
letzte-aber-eine Zeile).

Registrierung erfolgt in main.py neben dem bestehenden
services.autonomous_agent-Scheduler (gleiche apscheduler-Instanz-Bibliothek,
kein neues Paket).
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta

from sqlalchemy import select, desc

logger = logging.getLogger(__name__)

# Testbar in Minuten statt Tagen, siehe Bericht (Judgment Call).
REMINDER_DELAY_MINUTES = int(os.getenv("NEGOTIATION_REMINDER_DELAY_MINUTES", "15"))


async def poll_inbox_job():
    from services.email_imap import fetch_unseen_messages
    from database import AsyncSessionLocal
    from routers.negotiation import ingest_inbound_message

    try:
        messages = await asyncio.to_thread(fetch_unseen_messages)
    except Exception:
        logger.exception("poll_inbox_job: IMAP-Abruf fehlgeschlagen")
        return

    if not messages:
        return

    async with AsyncSessionLocal() as db:
        for m in messages:
            try:
                result = await ingest_inbound_message(db, m)
                logger.info(f"poll_inbox_job: Nachricht verarbeitet -> {result}")
            except Exception:
                logger.exception(f"poll_inbox_job: Verarbeitung einer Nachricht fehlgeschlagen: {m.get('subject')}")


async def check_reminders_job():
    """A5: 'Keine Antwort innerhalb des konfigurierten Fensters' -> ein
    Erinnerungsentwurf wird angelegt (status=draft), der genau wie ein
    Preisvorschlag menschliche Freigabe ueber POST .../approve braucht,
    bevor er tatsaechlich gesendet wird. Es wird hoechstens EIN Reminder
    pro Fall erzeugt (keine Dauerschleife)."""
    from database import AsyncSessionLocal
    from models_v2 import Case, CaseStatus
    from models_negotiation import (
        NegotiationStrategy, NegotiationAction, NegotiationActionType, NegotiationActionStatus,
        EmailMessage, EmailDirection, NegotiationException, NegotiationExceptionType,
    )

    cutoff = datetime.utcnow() - timedelta(minutes=REMINDER_DELAY_MINUTES)

    async with AsyncSessionLocal() as db:
        try:
            r = await db.execute(select(Case).where(Case.status == CaseStatus.WAITING_SUPPLIER))
            cases = r.scalars().all()
            for case in cases:
                r2 = await db.execute(
                    select(EmailMessage)
                    .where(EmailMessage.case_id == case.id, EmailMessage.direction == EmailDirection.outbound)
                    .order_by(desc(EmailMessage.occurred_at))
                )
                last_out = r2.scalars().first()
                if not last_out or last_out.occurred_at > cutoff:
                    continue

                existing_reminder = await db.execute(
                    select(NegotiationAction).where(
                        NegotiationAction.case_id == case.id,
                        NegotiationAction.action_type == NegotiationActionType.send_reminder,
                    )
                )
                if existing_reminder.scalars().first():
                    continue  # bereits ein Reminder fuer diesen Fall erzeugt

                strategy = (await db.execute(select(NegotiationStrategy).where(NegotiationStrategy.case_id == case.id))).scalar_one_or_none()
                if not strategy:
                    continue

                reminder = NegotiationAction(
                    case_id=case.id, tenant_id=case.tenant_id, round_number=0,
                    action_type=NegotiationActionType.send_reminder, proposed_price_net=None,
                    currency=strategy.currency, scope_change=False,
                    reason=f"Keine Antwort innerhalb von {REMINDER_DELAY_MINUTES} Minuten.",
                    open_questions=[], recipient_email=strategy.supplier_email,
                    rendered_subject=f"Erinnerung: {last_out.subject}",
                    rendered_body=(
                        "Guten Tag,\n\nwir moechten kurz an unsere vorherige Nachricht erinnern und freuen uns "
                        "auf Ihre Rueckmeldung.\n\nFreundliche Gruesse,\nNegotiateX – KI-gestuetzte "
                        "Verhandlungsassistenz.\nTestlauf, keine Beauftragung."
                    ),
                    status=NegotiationActionStatus.draft, case_version_at_draft=case.case_version,
                )
                db.add(reminder)
                db.add(NegotiationException(
                    case_id=case.id, tenant_id=case.tenant_id,
                    exception_type=NegotiationExceptionType.no_reply,
                    detail_text=f"Keine Antwort seit {last_out.occurred_at.isoformat()} (Fenster: {REMINDER_DELAY_MINUTES} min).",
                    resolution_note=None,
                ))
                logger.info(f"check_reminders_job: Reminder-Entwurf fuer Fall {case.id} angelegt (wartet auf Freigabe).")
            await db.commit()
        except Exception:
            logger.exception("check_reminders_job fehlgeschlagen")


def register_negotiation_jobs(scheduler):
    from apscheduler.triggers.interval import IntervalTrigger
    scheduler.add_job(
        poll_inbox_job, trigger=IntervalTrigger(seconds=60),
        id="negotiation_poll_inbox", name="Negotiation: IMAP-Posteingang abfragen",
        replace_existing=True,
    )
    scheduler.add_job(
        check_reminders_job, trigger=IntervalTrigger(minutes=2),
        id="negotiation_check_reminders", name="Negotiation: Reminder-Fenster pruefen",
        replace_existing=True,
    )
    logger.info("Negotiation-Scheduler-Jobs registriert (IMAP-Poll 60s, Reminder-Check 2min).")
