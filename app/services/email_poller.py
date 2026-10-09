"""
APScheduler-Jobs fuer Teil A + Teil B: (1) IMAP-Posteingang abfragen und
Antworten Faellen ODER Sourcing-/NDA-Threads zuordnen, (2) "keine Antwort
innerhalb des Fensters"-Pruefung fuer Verhandlungen (A5) und fuer
Erstkontakt-Outreach (B4, max. zwei Reminder).

Disambiguierung eingehender Antworten (Teil B Erweiterung, 9.10.2026):
`poll_inbox_job` versucht die Message-ID/In-Reply-To/References-Header EINER
Nachricht der Reihe nach gegen drei Tabellen zu matchen:
  1. models_negotiation.EmailMessage (outbound)   -> Verhandlungs-Antwort
  2. models_sourcing.NDA.sent_message_id            -> NDA-Ruecklauf
  3. models_sourcing.OutreachMessage (outbound)   -> Erstkontakt-Antwort
NDA wird VOR dem generischen Outreach-Treffer geprueft, weil eine NDA-Mail
im selben Kandidaten-Thread technisch auch auf eine OutreachMessage
antworten koennte (die Stammblatt-/NDA-Anfrage wird ja selbst als
OutreachMessage-Zeile gespeichert) -- ohne diese Reihenfolge wuerde ein
NDA-Ruecklauf faelschlich als gewoehnliche Outreach-Antwort klassifiziert
und NIE die Redline-Pruefung durchlaufen. Kein Treffer in keiner der drei
Tabellen -> Nachricht wird geloggt, aber nicht verworfen (kein Automatismus,
der still etwas annimmt).

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


async def _dispatch_inbound_message(db, m: dict) -> dict:
    """Siehe Moduldoc: versucht Negotiation -> NDA -> generische Outreach-
    Antwort, in dieser Reihenfolge. Jede Stufe prueft selbst per
    Message-ID/In-Reply-To/References, ob sie zustaendig ist, bevor sie
    etwas schreibt."""
    from sqlalchemy import select
    from models_negotiation import EmailMessage, EmailDirection
    from models_sourcing import NDA, NDAStatus, OutreachMessage, OutreachDirection
    from routers.negotiation import ingest_inbound_message
    from routers.sourcing import ingest_inbound_outreach_message
    from services.sourcing_classifier import detect_redline, detect_signature_claim
    from datetime import datetime as _dt

    in_reply_to = m.get("in_reply_to")
    refs = (m.get("references_header") or "").split()
    candidate_ids = set(t.strip() for t in refs if t.strip())
    if in_reply_to:
        candidate_ids.add(in_reply_to.strip())
    if not candidate_ids:
        logger.warning(f"poll_inbox_job: Nachricht ohne In-Reply-To/References, kann nicht zugeordnet werden: {m.get('subject')}")
        return {"matched": False}

    # 1) Verhandlung (Teil A)?
    r = await db.execute(select(EmailMessage).where(
        EmailMessage.direction == EmailDirection.outbound, EmailMessage.message_id.in_(list(candidate_ids)),
    ))
    if r.scalars().first():
        return await ingest_inbound_message(db, m)

    # 2) NDA-Ruecklauf (Teil B / B6)? -- NDA-Versandmails werden selbst als
    # OutreachMessage(outbound) gespeichert, muessen also VOR der generischen
    # Outreach-Antwort-Behandlung erkannt werden.
    r = await db.execute(select(NDA).where(NDA.sent_message_id.in_(list(candidate_ids)), NDA.status == NDAStatus.sent))
    nda = r.scalars().first()
    if nda:
        redline = detect_redline(nda.draft_text, m.get("body_text") or "")
        sig_claim = detect_signature_claim(m.get("body_text") or "")
        nda.returned_at = _dt.utcnow()
        nda.returned_text = m.get("body_text") or ""
        nda.redline_detected = redline
        nda.apparent_signature_claim = sig_claim
        from routers.sourcing import _nda_transition
        if redline:
            await _nda_transition(db, nda, NDAStatus.review_required, actor="system",
                                   reason="Ruecklauf per IMAP erhalten -- weicht vom versendeten Text ab (Redline), Human-Review/Legal erforderlich.")
        else:
            await _nda_transition(db, nda, NDAStatus.returned, actor="system",
                                   reason="Ruecklauf per IMAP erhalten -- identisch zum versendeten Text, bereit fuer manuelle Pruefung.")
        await db.commit()
        return {"matched": True, "kind": "nda_return", "redline_detected": redline}

    # 3) Generische Erstkontakt-/Stammblatt-Antwort (Teil B / B4)?
    r = await db.execute(select(OutreachMessage).where(
        OutreachMessage.direction == OutreachDirection.outbound, OutreachMessage.message_id.in_(list(candidate_ids)),
    ))
    if r.scalars().first():
        return await ingest_inbound_outreach_message(db, m)

    logger.warning(f"poll_inbox_job: Nachricht konnte keinem Fall/Kandidaten/NDA zugeordnet werden: {m.get('subject')}")
    return {"matched": False}


async def poll_inbox_job():
    from services.email_imap import fetch_unseen_messages
    from database import AsyncSessionLocal

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
                result = await _dispatch_inbound_message(db, m)
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


async def check_outreach_reminders_job():
    """B4: 'hoechstens ZWEI Reminder auf einem Timer'. Faellige, nicht
    stornierte OutreachReminderTimer-Zeilen (siehe models_sourcing --
    stornes werden von ingest_inbound_outreach_message bei JEDER
    eingehenden Antwort gesetzt) erzeugen einen Reminder-Entwurf, der
    genau wie der Erstkontakt menschliche Freigabe ueber
    /outreach/{id}/approve braucht, bevor er gesendet wird."""
    from database import AsyncSessionLocal
    from sqlalchemy import select
    from models_sourcing import (
        OutreachReminderTimer, OutreachAction, OutreachActionStatus, SupplierCandidate, CandidateStatus,
    )
    from datetime import datetime as _dt

    async with AsyncSessionLocal() as db:
        try:
            r = await db.execute(select(OutreachReminderTimer).where(
                OutreachReminderTimer.fired == False,  # noqa: E712
                OutreachReminderTimer.cancelled == False,  # noqa: E712
                OutreachReminderTimer.due_at <= _dt.utcnow(),
            ))
            due_timers = r.scalars().all()
            for timer in due_timers:
                cand = (await db.execute(select(SupplierCandidate).where(SupplierCandidate.id == timer.supplier_candidate_id))).scalar_one_or_none()
                if not cand or cand.status != CandidateStatus.contacted:
                    # Kandidat hat bereits geantwortet oder Status geaendert
                    # -> Timer stornieren statt Reminder zu erzeugen.
                    timer.fired = True
                    continue
                reminder = OutreachAction(
                    tenant_id=timer.tenant_id, supplier_candidate_id=cand.id, kind="reminder",
                    reminder_number=timer.reminder_number, recipient_email=cand.contact_email,
                    rendered_subject=f"Erinnerung ({timer.reminder_number}/2): Anfrage zu {cand.service_match_note or cand.company_name}",
                    rendered_body=(
                        "Guten Tag,\n\nwir moechten kurz an unsere vorherige Anfrage erinnern und freuen uns "
                        "auf Ihre Rueckmeldung, ob grundsaetzlich Interesse an einer Angebotsabgabe besteht.\n\n"
                        "Freundliche Gruesse,\nNegotiateX – KI-gestuetzte Beschaffungskoordination.\nTestlauf."
                    ),
                    status=OutreachActionStatus.draft,
                )
                db.add(reminder)
                timer.fired = True
                logger.info(f"check_outreach_reminders_job: Reminder {timer.reminder_number} fuer Kandidat {cand.id} angelegt (wartet auf Freigabe).")
            await db.commit()
        except Exception:
            logger.exception("check_outreach_reminders_job fehlgeschlagen")


def register_negotiation_jobs(scheduler):
    from apscheduler.triggers.interval import IntervalTrigger
    scheduler.add_job(
        poll_inbox_job, trigger=IntervalTrigger(seconds=60),
        id="negotiation_poll_inbox", name="Negotiation+Sourcing: IMAP-Posteingang abfragen",
        replace_existing=True,
    )
    scheduler.add_job(
        check_reminders_job, trigger=IntervalTrigger(minutes=2),
        id="negotiation_check_reminders", name="Negotiation: Reminder-Fenster pruefen",
        replace_existing=True,
    )
    scheduler.add_job(
        check_outreach_reminders_job, trigger=IntervalTrigger(minutes=1),
        id="sourcing_check_outreach_reminders", name="Sourcing: Outreach-Reminder-Timer pruefen",
        replace_existing=True,
    )
    logger.info("Negotiation+Sourcing-Scheduler-Jobs registriert (IMAP-Poll 60s, Negotiation-Reminder 2min, Outreach-Reminder 1min).")
