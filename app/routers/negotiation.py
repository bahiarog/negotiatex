"""
Teil A des Agenten-Playbooks: Zwei-Runden-Preisverhandlung per E-Mail mit
menschlicher Freigabe vor jedem Versand.

Kernprinzip (gleiche Sicherheitsphilosophie wie routers/chat.py): Der
Anthropic-Call hier bekommt NIE ein `tools`-Argument. Das Modell liefert nur
Text (die E-Mail-Formulierung) bzw. im Klassifikator einen von acht festen
Strings. Der Preis wird NIE vom Modell gewaehlt, sondern immer
deterministisch aus der gespeicherten NegotiationStrategy gelesen. Nur ein
expliziter, menschlich ausgeloester Aufruf von POST .../approve sendet
tatsaechlich eine E-Mail oder aendert den Fall-Status in Richtung Versand.

Tenant-Isolation: identisches Muster wie routers/cases.py (_get_case_or_404,
immer ueber membership.tenant_id gefiltert, unbekannte/fremde Faelle -> 404).
"""
import hashlib
import logging
import re
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from email.utils import make_msgid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from deps import get_current_user, get_current_membership
from models_v2 import Case, CaseStatus
from models_negotiation import (
    NegotiationStrategy, NegotiationAction, NegotiationActionType, NegotiationActionStatus,
    NegotiationApproval, EmailMessage, EmailDirection, NegotiationException, NegotiationExceptionType,
)
from routers.cases import _transition, _get_case_or_404
from services.email_sender import send_negotiation_email
from services.negotiation_classifier import classify_reply, _extract_prices

logger = logging.getLogger(__name__)
router = APIRouter()

# Rot-Flaggen fuer den Post-Render-Text-Check (A2). Bewusst eine feste,
# einfache Stichwortliste -- dies ist ein Best-Effort-Textscan, KEINE formale
# Garantie, dass der generierte Text frei von unzulaessigen Inhalten ist.
_FORBIDDEN_PHRASES = [
    "konkurrenzangebot", "wettbewerber bietet", "anderer anbieter bietet",
    "dringend", "dringlichkeit", "zeitdruck", "schnell entscheiden",
    "folgeauftrag", "weitere auftraege in aussicht", "zukuenftige auftraege",
    "rabatt von", "sonderrabatt", "bereits gewaehrten rabatt",
]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class StrategyCreate(BaseModel):
    starting_price_net: Decimal
    round1_price_net: Decimal
    round2_price_net: Decimal
    price_cap_net: Decimal
    currency: str = "EUR"
    scope_text: str
    usage_rights_text: str
    delivery_date: str
    max_rounds: int = 2
    supplier_email: str


class ApprovalRequest(BaseModel):
    note: Optional[str] = None


class RejectRequest(BaseModel):
    reason: Optional[str] = None


class TestInboundReply(BaseModel):
    """Nur fuer verifizierte Tests / als Fallback, wenn kein zweites reales
    Postfach zum Antworten zur Verfuegung steht. Simuliert exakt das, was
    der IMAP-Poller aus einer echten Antwort erzeugen wuerde. JEDE Nutzung
    dieses Endpunkts muss im Bericht klar als 'simuliert' gekennzeichnet
    werden, nicht als echter IMAP-Roundtrip."""
    from_addr: str
    subject: str
    body_text: str
    in_reply_to: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_strategy_or_404(case: Case, db: AsyncSession) -> NegotiationStrategy:
    r = await db.execute(select(NegotiationStrategy).where(NegotiationStrategy.case_id == case.id))
    strategy = r.scalar_one_or_none()
    if not strategy:
        raise HTTPException(404, "Keine Verhandlungsstrategie fuer diesen Fall hinterlegt.")
    return strategy


async def _get_action_or_404(case: Case, action_id: str, db: AsyncSession) -> NegotiationAction:
    try:
        aid = uuid.UUID(action_id)
    except ValueError:
        raise HTTPException(404, "Aktion nicht gefunden.")
    r = await db.execute(select(NegotiationAction).where(NegotiationAction.id == aid))
    action = r.scalar_one_or_none()
    if not action or action.case_id != case.id:
        raise HTTPException(404, "Aktion nicht gefunden.")
    return action


def _compute_payload_hash(recipient: str, subject: str, body: str, price, case_version: int) -> str:
    """Sha256 ueber recipient+subject+body+price+case_version. Dies ist das
    Kernstueck der Freigabe-Bindung ("jede Aenderung an Text, Konditionen
    oder Empfaenger macht die Freigabe ungueltig"): vor dem tatsaechlichen
    Versand wird dieser Hash aus dem, was WIRKLICH gesendet werden soll,
    neu berechnet und mit dem bei Freigabe gespeicherten Hash verglichen."""
    raw = f"{recipient}|{subject}|{body}|{price}|{case_version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _count_sent_price_rounds(case_id, db: AsyncSession) -> int:
    r = await db.execute(
        select(NegotiationAction).where(
            NegotiationAction.case_id == case_id,
            NegotiationAction.action_type == NegotiationActionType.propose_price,
            NegotiationAction.status == NegotiationActionStatus.sent,
        )
    )
    return len(r.scalars().all())


async def _latest_inbound_price(case_id, db: AsyncSession) -> Optional[Decimal]:
    r = await db.execute(
        select(EmailMessage)
        .where(EmailMessage.case_id == case_id, EmailMessage.direction == EmailDirection.inbound)
        .order_by(desc(EmailMessage.occurred_at))
    )
    latest = r.scalars().first()
    if not latest:
        return None
    prices = _extract_prices(latest.body_text or "")
    # Nimmt den LETZTEN im Text genannten Preis, nicht den ersten: typische
    # Antwortstruktur ist "X ist nicht moeglich. Wir bieten Y." -- Y (das
    # tatsaechliche Gegenangebot) steht meist am Ende, X (unser vorheriger
    # Vorschlag, den der Lieferant zitiert) zuerst.
    return prices[-1] if prices else None


def _pre_render_check(strategy: NegotiationStrategy, round_number: int, price: Decimal) -> list[str]:
    """Prueft die STRUKTURIERTE Aktion gegen die gespeicherte Strategie,
    BEVOR irgendein Text generiert wird. Quote: 'Preis entspricht der
    genehmigten Strategie; Pflichtleistungen, Rechte und Termin bleiben
    erhalten.'"""
    problems = []
    if round_number > strategy.max_rounds:
        problems.append(f"Rundenzahl {round_number} ueberschreitet max_rounds={strategy.max_rounds}.")
    if price > strategy.price_cap_net:
        problems.append(f"Preis {price} ueberschreitet die harte Preisobergrenze {strategy.price_cap_net}.")
    expected = strategy.round1_price_net if round_number == 1 else strategy.round2_price_net
    if Decimal(str(price)) != Decimal(str(expected)):
        problems.append(f"Preis {price} entspricht nicht dem strategiefestgelegten Rundenpreis {expected}.")
    return problems


def _post_render_check(body: str, price: Decimal, strategy: NegotiationStrategy) -> list[str]:
    """Best-Effort-Textscan NACH der Textgenerierung. Das ist KEINE formale
    Garantie, sondern eine zusaetzliche Plausibilitaetspruefung: (a) taucht
    der freigegebene Preis als Zahl im Text auf, (b) enthaelt der Text keine
    der festen Rot-Flaggen-Phrasen (erfundene Konkurrenzangebote,
    Folgeauftraege, Dringlichkeit, nicht gewaehrte Rabatte)."""
    problems = []
    low = body.lower()

    price_str_variants = {
        f"{price:,.0f}".replace(",", "."),            # 10.500
        f"{price:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),  # 10.500,00
        str(int(price)) if price == price.to_integral_value() else str(price),
    }
    if not any(v in body for v in price_str_variants):
        problems.append(f"Preis {price} erscheint nicht erkennbar im generierten Text.")

    for phrase in _FORBIDDEN_PHRASES:
        if phrase in low:
            problems.append(f"Verbotene Formulierung im Text gefunden: '{phrase}'.")

    return problems


EMAIL_DRAFT_SYSTEM_PROMPT = """Du formulierst eine einzelne Verhandlungs-E-Mail auf Deutsch im Namen \
von NegotiateX, einer KI-gestuetzten Verhandlungsassistenz, im Testlauf-Modus. Du hast KEINE \
Werkzeuge und loest selbst nichts aus -- du lieferst ausschliesslich den E-Mail-Text. Ein Mensch \
muss die E-Mail getrennt freigeben, bevor sie je versendet wird.

Regeln, die du NIEMALS verletzen darfst:
- Nenne EXAKT den vorgegebenen Preis, keinen anderen.
- Erwaehne Leistungsumfang, Nutzungsrechte und Liefertermin unveraendert wie vorgegeben (inhaltlich, \
nicht zwingend wortgleich, aber ohne sie zu veraendern oder wegzulassen).
- Erfinde NIEMALS Konkurrenzangebote, Folgeauftraege, Dringlichkeit/Zeitdruck oder bereits gewaehrte \
Rabatte.
- Schreibe sachlich, hoeflich, kurz (max. ca. 150 Woerter), im Register geschaeftlicher \
Einkaufskorrespondenz.
- Beende die E-Mail mit dem Hinweis 'Testlauf, keine Beauftragung.' und der Signatur \
'NegotiateX – KI-gestuetzte Verhandlungsassistenz.'

Antworte AUSSCHLIESSLICH als JSON-Objekt mit genau den Feldern "subject" und "body", ohne \
Markdown-Codeblock, ohne Erklaerung."""


def _render_email_text(case: Case, strategy: NegotiationStrategy, round_number: int, price: Decimal,
                        counter_price: Optional[Decimal]) -> tuple[str, str]:
    """Ruft Anthropic auf (KEIN tools=), um Betreff+Text zu formulieren.
    Faellt bei jedem Fehler ODER wenn der Post-Render-Check fehlschlaegt auf
    eine deterministische Vorlage zurueck (Wortlaut aus A3/A4 des
    Playbooks), damit das gespeicherte Draft garantiert korrekt ist."""
    import json as _json
    import os as _os

    def _fmt(n: Decimal) -> str:
        # Deutsches Zahlenformat (Punkt als Tausendertrennzeichen), OHNE die
        # restlichen Satzzeichen des Textes zu beeinflussen -- bewusst
        # NICHT per globalem str.replace(",", ".") auf den ganzen Satz
        # angewendet (das wuerde auch Aufzaehlungskommas in scope_text
        # zerstoeren).
        return f"{n:,.0f}".replace(",", ".")

    subject_fallback = f"TEST – Rueckfrage zu Angebot {case.title}"
    if round_number == 1:
        body_fallback = (
            f"Guten Tag,\n\nvielen Dank fuer Ihr Angebot ueber {_fmt(strategy.starting_price_net)} € netto "
            f"fuer die beschriebene Leistung ({strategy.scope_text}). Koennen Sie die Leistung zu einem "
            f"Gesamtpreis von {_fmt(price)} € netto anbieten? Der vereinbarte Leistungsumfang "
            f"({strategy.scope_text}), die Nutzungsrechte ({strategy.usage_rights_text}) und der "
            f"Liefertermin ({strategy.delivery_date}) sollen dabei unveraendert bleiben. Bitte teilen Sie "
            f"uns mit, ob diese Konditionen moeglich sind oder welches Gegenangebot Sie bei unverandertem "
            f"Umfang machen koennen.\n\nFreundliche Gruesse,\nNegotiateX – KI-gestuetzte "
            f"Verhandlungsassistenz.\nTestlauf, keine Beauftragung."
        )
    else:
        counter_txt = f"{_fmt(counter_price)} €" if counter_price else "Ihr Gegenangebot"
        body_fallback = (
            f"Guten Tag,\n\nvielen Dank fuer Ihre Rueckmeldung und das Gegenangebot ueber {counter_txt} "
            f"netto. Koennen Sie die Leistung zu einem Gesamtpreis von {_fmt(price)} € netto anbieten? Der "
            f"vereinbarte Leistungsumfang ({strategy.scope_text}), die Nutzungsrechte "
            f"({strategy.usage_rights_text}) und der Liefertermin ({strategy.delivery_date}) sollen dabei "
            f"unveraendert bleiben. Dies ist unser letzter Vorschlag in diesem Testlauf; die Entscheidung "
            f"ueber Ihre Antwort liegt anschliessend beim Auftraggeber.\n\nFreundliche Gruesse,\nNegotiateX "
            f"– KI-gestuetzte Verhandlungsassistenz.\nTestlauf, keine Beauftragung."
        )

    api_key = _os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return subject_fallback, body_fallback

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        user_prompt = (
            f"Runde: {round_number} von max. {strategy.max_rounds}\n"
            f"Fall-Titel: {case.title}\n"
            f"Ausgangsangebot des Lieferanten: {strategy.starting_price_net} {strategy.currency} netto\n"
            f"Unser Preisvorschlag in dieser Runde: {price} {strategy.currency} netto\n"
            + (f"Vom Lieferanten erhaltenes Gegenangebot: {counter_price} {strategy.currency} netto\n" if counter_price else "")
            + f"Leistungsumfang (unveraenderlich): {strategy.scope_text}\n"
            f"Nutzungsrechte (unveraenderlich): {strategy.usage_rights_text}\n"
            f"Liefertermin (unveraenderlich): {strategy.delivery_date}\n"
        )
        # Bewusst KEIN `tools=` Parameter -- das Modell liefert nur Text.
        resp = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=1000,
            thinking={"type": "disabled"},  # sonst verbraucht das Nachdenken das Token-Budget, Antwort bliebe leer
            system=EMAIL_DRAFT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
        raw = re.sub(r"```(?:json)?", "", raw).strip("` \n")
        data = _json.loads(raw)
        subject = str(data.get("subject") or subject_fallback)
        body = str(data.get("body") or body_fallback)

        if _post_render_check(body, price, strategy):
            logger.warning("Negotiation draft: LLM-Text bestand Post-Render-Check nicht -> Fallback-Vorlage verwendet.")
            return subject_fallback, body_fallback
        return subject, body
    except Exception:
        logger.exception("Negotiation draft: LLM-Rendering fehlgeschlagen -> Fallback-Vorlage verwendet.")
        return subject_fallback, body_fallback


def _action_to_dict(a: NegotiationAction, strategy: NegotiationStrategy) -> dict:
    return {
        "id": str(a.id),
        "case_id": str(a.case_id),
        "round_number": a.round_number,
        "action_type": a.action_type.value if hasattr(a.action_type, "value") else a.action_type,
        "proposed_price_net": str(a.proposed_price_net) if a.proposed_price_net is not None else None,
        "currency": a.currency,
        "scope_change": a.scope_change,
        "reason": a.reason,
        "open_questions": a.open_questions or [],
        "recipient_email": a.recipient_email,
        "rendered_subject": a.rendered_subject,
        "rendered_body": a.rendered_body,
        "payload_hash": a.payload_hash,
        "status": a.status.value if hasattr(a.status, "value") else a.status,
        "immutable_terms": {
            "scope_text": strategy.scope_text,
            "usage_rights_text": strategy.usage_rights_text,
            "delivery_date": strategy.delivery_date,
        },
        "source_references": {
            "strategy_id": str(strategy.id),
            "case_version_at_draft": a.case_version_at_draft,
        },
        "created_at": a.created_at,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/cases/{case_id}/strategy")
async def create_strategy(
    case_id: str,
    payload: StrategyCreate,
    user=Depends(get_current_user),
    membership=Depends(get_current_membership),
    db: AsyncSession = Depends(get_db),
):
    case = await _get_case_or_404(case_id, membership.tenant_id, db)

    if payload.round1_price_net > payload.price_cap_net or payload.round2_price_net > payload.price_cap_net:
        raise HTTPException(400, "Rundenpreise duerfen die Preisobergrenze (price_cap_net) nicht ueberschreiten.")
    if payload.round1_price_net > payload.starting_price_net:
        raise HTTPException(400, "Runde-1-Preis darf nicht ueber dem Ausgangsangebot liegen.")

    r = await db.execute(select(NegotiationStrategy).where(NegotiationStrategy.case_id == case.id))
    if r.scalar_one_or_none():
        raise HTTPException(400, "Fuer diesen Fall existiert bereits eine Strategie.")

    strategy = NegotiationStrategy(
        tenant_id=membership.tenant_id, case_id=case.id,
        starting_price_net=payload.starting_price_net, round1_price_net=payload.round1_price_net,
        round2_price_net=payload.round2_price_net, price_cap_net=payload.price_cap_net,
        currency=payload.currency, scope_text=payload.scope_text,
        usage_rights_text=payload.usage_rights_text, delivery_date=payload.delivery_date,
        max_rounds=payload.max_rounds, supplier_email=payload.supplier_email.strip().lower(),
        created_by=str(user.id),
    )
    db.add(strategy)
    await _transition(db, case, CaseStatus.READY_TO_DRAFT, actor=str(user.id),
                       reason="Verhandlungsstrategie bestaetigt (Ausgangslage und Regeln).")
    await db.commit()
    await db.refresh(strategy)
    return {"strategy_id": str(strategy.id), "case_status": case.status.value}


@router.post("/cases/{case_id}/actions/draft")
async def draft_action(
    case_id: str,
    user=Depends(get_current_user),
    membership=Depends(get_current_membership),
    db: AsyncSession = Depends(get_db),
):
    case = await _get_case_or_404(case_id, membership.tenant_id, db)
    strategy = await _get_strategy_or_404(case, db)

    open_exc = await db.execute(
        select(NegotiationException).where(NegotiationException.case_id == case.id, NegotiationException.resolved == False)  # noqa: E712
    )
    if open_exc.scalars().first():
        raise HTTPException(409, "Offene Ausnahme fuer diesen Fall -- erst menschliche Pruefung erforderlich, bevor ein neuer Entwurf erstellt werden kann.")

    round_number = await _count_sent_price_rounds(case.id, db) + 1
    if round_number > strategy.max_rounds:
        raise HTTPException(400, "Maximale Anzahl Preisvorschlaege erreicht. Kein weiterer Entwurf -- bitte Entscheidungsvorlage (/decision) nutzen.")

    price = strategy.round1_price_net if round_number == 1 else strategy.round2_price_net

    # A2: Strukturierte Aktion VOR jeder Textgenerierung.
    structured_action = {
        "case_id": str(case.id), "action": "propose_price",
        "proposed_price_net": str(price), "currency": strategy.currency,
        "scope_change": False, "requires_approval": True,
        "reason": f"Freigegebener {'erster' if round_number == 1 else 'zweiter'} Preisvorschlag",
        "open_questions": [],
    }
    pre_problems = _pre_render_check(strategy, round_number, price)
    if pre_problems:
        # Darf nie passieren, da Preis deterministisch aus der Strategie kommt --
        # trotzdem hart blockieren, falls doch (z.B. durch Dateninkonsistenz).
        raise HTTPException(400, "Pre-Render-Policy-Check fehlgeschlagen: " + "; ".join(pre_problems))

    counter_price = await _latest_inbound_price(case.id, db) if round_number > 1 else None
    subject, body = _render_email_text(case, strategy, round_number, price, counter_price)
    post_problems = _post_render_check(body, price, strategy)

    action = NegotiationAction(
        case_id=case.id, tenant_id=membership.tenant_id, round_number=round_number,
        action_type=NegotiationActionType.propose_price, proposed_price_net=price,
        currency=strategy.currency, scope_change=False,
        reason=structured_action["reason"], open_questions=post_problems,
        recipient_email=strategy.supplier_email, rendered_subject=subject, rendered_body=body,
        status=NegotiationActionStatus.draft, case_version_at_draft=case.case_version,
    )
    db.add(action)
    await _transition(db, case, CaseStatus.AWAITING_APPROVAL, actor=str(user.id),
                       reason=f"Entwurf Runde {round_number} erstellt -- wartet auf Freigabe.")
    await db.commit()
    await db.refresh(action)

    result = _action_to_dict(action, strategy)
    result["structured_action"] = structured_action
    result["post_render_warnings"] = post_problems
    return result


@router.get("/cases/{case_id}/actions/{action_id}")
async def get_action(
    case_id: str, action_id: str,
    membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db),
):
    case = await _get_case_or_404(case_id, membership.tenant_id, db)
    strategy = await _get_strategy_or_404(case, db)
    action = await _get_action_or_404(case, action_id, db)
    return _action_to_dict(action, strategy)


@router.post("/cases/{case_id}/actions/{action_id}/approve")
async def approve_action(
    case_id: str, action_id: str, payload: ApprovalRequest,
    user=Depends(get_current_user), membership=Depends(get_current_membership),
    db: AsyncSession = Depends(get_db),
):
    case = await _get_case_or_404(case_id, membership.tenant_id, db)
    strategy = await _get_strategy_or_404(case, db)
    action = await _get_action_or_404(case, action_id, db)

    if action.status == NegotiationActionStatus.draft:
        # Hash ueber genau das, was jetzt gesendet werden SOLL.
        h = _compute_payload_hash(action.recipient_email, action.rendered_subject, action.rendered_body,
                                   action.proposed_price_net, case.case_version)
        action.payload_hash = h
        action.status = NegotiationActionStatus.approved
        db.add(NegotiationApproval(action_id=action.id, approved_by=str(user.id), payload_hash=h))
        await db.commit()
    elif action.status != NegotiationActionStatus.approved:
        raise HTTPException(400, f"Aktion im Status '{action.status.value}' kann nicht freigegeben/gesendet werden.")

    # "Recompute immediately before calling send_negotiation_email": frische
    # Kopie der Aktion aus der DB lesen und den Hash aus IHREM aktuellen
    # Inhalt neu berechnen. Weicht er vom bei Freigabe gespeicherten Hash ab,
    # wird der Versand verweigert -- das macht "jede Aenderung macht die
    # Freigabe ungueltig" technisch real statt nur dokumentiert.
    await db.refresh(action)
    recomputed = _compute_payload_hash(action.recipient_email, action.rendered_subject, action.rendered_body,
                                        action.proposed_price_net, case.case_version)
    if recomputed != action.payload_hash:
        raise HTTPException(
            400,
            "Freigabe ungueltig: Inhalt der Aktion wurde nach der Freigabe veraendert "
            "(Hash-Mismatch). Versand verweigert. Bitte Fall pruefen und ggf. neu entwerfen.",
        )

    send_result = send_negotiation_email(
        to_email=action.recipient_email, subject=action.rendered_subject,
        body=action.rendered_body, from_name="NegotiateX.ai",
    )
    if not send_result.get("sent"):
        raise HTTPException(502, f"Versand fehlgeschlagen: {send_result.get('message')}")

    msg_id = send_result["message_id"]
    db.add(EmailMessage(
        case_id=case.id, tenant_id=membership.tenant_id, direction=EmailDirection.outbound,
        message_id=msg_id, from_addr="info@negotiatex.ai", to_addr=action.recipient_email,
        subject=action.rendered_subject, body_text=action.rendered_body,
        raw_source=None,
    ))
    action.status = NegotiationActionStatus.sent
    await _transition(db, case, CaseStatus.SEND_PENDING, actor=str(user.id), reason="Freigegeben, Versand ausgeloest.")
    await _transition(db, case, CaseStatus.WAITING_SUPPLIER, actor="system", reason="E-Mail erfolgreich versendet.")
    await db.commit()

    return {"sent": True, "message_id": msg_id, "case_status": case.status.value, "payload_hash": action.payload_hash}


@router.post("/cases/{case_id}/actions/{action_id}/reject")
async def reject_action(
    case_id: str, action_id: str, payload: RejectRequest,
    user=Depends(get_current_user), membership=Depends(get_current_membership),
    db: AsyncSession = Depends(get_db),
):
    case = await _get_case_or_404(case_id, membership.tenant_id, db)
    action = await _get_action_or_404(case, action_id, db)
    if action.status not in (NegotiationActionStatus.draft, NegotiationActionStatus.approved):
        raise HTTPException(400, f"Aktion im Status '{action.status.value}' kann nicht abgelehnt werden.")

    action.status = NegotiationActionStatus.rejected
    # Judgment call (siehe Bericht): Fall geht zurueck nach READY_TO_DRAFT,
    # nicht AWAITING_APPROVAL -- "Freigabe verweigern" bedeutet hier
    # "bitte neu entwerfen", nicht "Fall haengt in der Warteschlange fest".
    await _transition(db, case, CaseStatus.READY_TO_DRAFT, actor=str(user.id),
                       reason=f"Freigabe verweigert: {payload.reason or 'kein Grund angegeben'}")
    await db.commit()
    return {"status": "rejected", "case_status": case.status.value}


@router.get("/cases/{case_id}/decision")
async def get_decision(
    case_id: str,
    membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db),
):
    case = await _get_case_or_404(case_id, membership.tenant_id, db)
    strategy = await _get_strategy_or_404(case, db)

    latest_price = await _latest_inbound_price(case.id, db)
    open_scope_exc = await db.execute(
        select(NegotiationException).where(
            NegotiationException.case_id == case.id,
            NegotiationException.exception_type == NegotiationExceptionType.scope_change,
            NegotiationException.resolved == False,  # noqa: E712
        )
    )
    scope_evidenced_unchanged = open_scope_exc.scalars().first() is None

    improvement_abs = None
    improvement_pct = None
    within_cap = None
    if latest_price is not None:
        start = Decimal(str(strategy.starting_price_net))
        improvement_abs = start - latest_price
        improvement_pct = float((improvement_abs / start) * 100) if start else None
        within_cap = latest_price <= Decimal(str(strategy.price_cap_net))

    return {
        "case_id": str(case.id),
        "starting_price_net": str(strategy.starting_price_net),
        "latest_counter_offer_net": str(latest_price) if latest_price is not None else None,
        "improvement_eur": str(improvement_abs) if improvement_abs is not None else None,
        "improvement_pct": round(improvement_pct, 2) if improvement_pct is not None else None,
        "within_cap": within_cap,
        "price_cap_net": str(strategy.price_cap_net),
        "scope_evidenced_unchanged": scope_evidenced_unchanged,
        "next_step": "human_decision",
        "note": "Kein automatischer Abschluss -- die Entscheidung liegt beim Auftraggeber.",
    }


@router.get("/cases/{case_id}/emails")
async def get_case_emails(
    case_id: str,
    membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db),
):
    case = await _get_case_or_404(case_id, membership.tenant_id, db)
    r = await db.execute(select(EmailMessage).where(EmailMessage.case_id == case.id).order_by(EmailMessage.occurred_at))
    return [
        {
            "id": str(m.id), "direction": m.direction.value if hasattr(m.direction, "value") else m.direction,
            "message_id": m.message_id, "from_addr": m.from_addr, "to_addr": m.to_addr,
            "subject": m.subject, "body_text": m.body_text, "occurred_at": m.occurred_at,
        }
        for m in r.scalars().all()
    ]


@router.get("/cases/{case_id}/exceptions")
async def get_case_exceptions(
    case_id: str,
    membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db),
):
    case = await _get_case_or_404(case_id, membership.tenant_id, db)
    r = await db.execute(select(NegotiationException).where(NegotiationException.case_id == case.id).order_by(desc(NegotiationException.detected_at)))
    return [
        {
            "id": str(e.id), "exception_type": e.exception_type.value if hasattr(e.exception_type, "value") else e.exception_type,
            "detail_text": e.detail_text, "resolved": e.resolved, "resolution_note": e.resolution_note,
            "detected_at": e.detected_at,
        }
        for e in r.scalars().all()
    ]


# ---------------------------------------------------------------------------
# Inbound reply handling -- shared by IMAP poller (services/email_poller.py)
# and the test-inject endpoint below. NICHT oeffentlich: dieser Pfad
# entscheidet selbst nichts, er klassifiziert und legt im Zweifel eine
# Ausnahme + PAUSED an; nur ein Mensch kann ueber die oben stehenden
# Endpunkte danach weiter agieren.
# ---------------------------------------------------------------------------

async def _project_event_for_case(db: AsyncSession, case: Case, classification: str, body: str):
    """Verhandlungsantwort im Zeitstrahl eines Vorhabens melden (falls der
    Fall zu einem gehoert). Fehler brechen die Verarbeitung nie ab."""
    try:
        from sqlalchemy import cast, String
        from models_projects import Project
        from services.projects import add_event
        project = (await db.execute(select(Project).where(
            cast(Project.negotiations_json, String).like(f"%{case.id}%")))).scalars().first()
        if not project:
            return
        prices = _extract_prices(body or "")
        what = {"acceptance": "hat den Vorschlag angenommen", "counter_price": "hat ein Gegenangebot gemacht",
                "no_movement": "bleibt beim bisherigen Preis"}.get(classification, "hat geantwortet")
        await add_event(db, project, "negotiation_reply", f"Verhandlung: Dienstleister {what}",
                        f"Genannter Preis: {prices[-1]}" if prices else None, milestone=True)
    except Exception:
        logger.exception(f"Vorhaben-Ereignis fuer Fall {case.id} fehlgeschlagen")


async def ingest_inbound_message(db: AsyncSession, email_record: dict) -> dict:
    """email_record: dict wie von services.email_imap.fetch_unseen_messages
    geliefert (oder vom Test-Endpunkt synthetisiert). Matched via Message-ID-
    Header (In-Reply-To/References) gegen gespeicherte outbound EmailMessage-
    Zeilen -- NICHT ueber den Betreff."""
    in_reply_to = email_record.get("in_reply_to")
    refs = email_record.get("references_header") or ""
    candidate_ids = set()
    if in_reply_to:
        candidate_ids.add(in_reply_to.strip())
    for token in refs.split():
        candidate_ids.add(token.strip())

    matched_outbound = None
    if candidate_ids:
        r = await db.execute(
            select(EmailMessage).where(
                EmailMessage.direction == EmailDirection.outbound,
                EmailMessage.message_id.in_(list(candidate_ids)),
            )
        )
        matched_outbound = r.scalars().first()

    if not matched_outbound:
        logger.warning(f"Inbound-Mail konnte keinem Fall zugeordnet werden (Message-ID/References ohne Treffer): {email_record.get('subject')}")
        return {"matched": False}

    case_id = matched_outbound.case_id
    tenant_id = matched_outbound.tenant_id

    r = await db.execute(select(Case).where(Case.id == case_id))
    case = r.scalar_one_or_none()
    if not case:
        return {"matched": False}
    strategy = (await db.execute(select(NegotiationStrategy).where(NegotiationStrategy.case_id == case.id))).scalar_one_or_none()
    if not strategy:
        return {"matched": False}

    # Idempotenz: message_id ist UNIQUE -- doppeltes Verarbeiten derselben
    # Nachricht (z.B. erneuter Poll) wird hier abgefangen statt zu crashen.
    if email_record.get("message_id"):
        existing = await db.execute(select(EmailMessage).where(EmailMessage.message_id == email_record["message_id"]))
        if existing.scalar_one_or_none():
            return {"matched": True, "duplicate": True}

    from_addr = (email_record.get("from_addr") or "").strip().lower()

    inbound = EmailMessage(
        case_id=case.id, tenant_id=tenant_id, direction=EmailDirection.inbound,
        message_id=email_record.get("message_id") or f"<generated-{uuid.uuid4()}@negotiatex.ai>",
        in_reply_to=in_reply_to, references_header=refs or None,
        from_addr=from_addr, to_addr=email_record.get("to_addr") or "info@negotiatex.ai",
        subject=email_record.get("subject"), body_text=email_record.get("body_text") or "",
        raw_source=email_record.get("raw_source"),
    )
    db.add(inbound)
    await db.flush()

    # A5 Zeile 7: Absenderidentitaet zuerst pruefen, VOR jeder Klassifikation.
    if from_addr != strategy.supplier_email:
        db.add(NegotiationException(
            case_id=case.id, tenant_id=tenant_id,
            exception_type=NegotiationExceptionType.unknown_sender,
            detail_text=f"Antwort von unerwarteter Adresse '{from_addr}' (erwartet: '{strategy.supplier_email}').",
            source_email_message_id=inbound.id,
        ))
        await _transition(db, case, CaseStatus.PAUSED, actor="system",
                           reason="Unbekannter Absender -- Workflow angehalten, manuelle Pruefung erforderlich.")
        await db.commit()
        return {"matched": True, "classification": "unknown_sender"}

    classification = classify_reply(case, strategy, inbound.body_text)

    if classification in ("scope_change", "new_commitment", "clarifying_question", "prompt_injection_attempt"):
        exc_type = NegotiationExceptionType(classification)
        db.add(NegotiationException(
            case_id=case.id, tenant_id=tenant_id, exception_type=exc_type,
            detail_text=inbound.body_text[:2000], source_email_message_id=inbound.id,
        ))
        await _transition(db, case, CaseStatus.PAUSED, actor="system",
                           reason=f"Ausnahme erkannt ({classification}) -- Workflow angehalten, menschliche Entscheidung erforderlich.")
        await db.commit()
        return {"matched": True, "classification": classification}

    # counter_price / acceptance / no_movement: zaehlt als Preisrunde.
    await _project_event_for_case(db, case, classification, inbound.body_text)
    rounds_sent = await _count_sent_price_rounds(case.id, db)
    await _transition(db, case, CaseStatus.EVALUATING_RESPONSE, actor="system",
                       reason=f"Antwort erhalten und klassifiziert als '{classification}'.")

    if classification == "acceptance" or rounds_sent >= strategy.max_rounds:
        await _transition(db, case, CaseStatus.READY_FOR_DECISION, actor="system",
                           reason="Max. Rundenzahl erreicht oder Annahme gemeldet -- Entscheidungsvorlage bereit, kein automatischer Abschluss.")
    else:
        await _transition(db, case, CaseStatus.READY_TO_DRAFT, actor="system",
                           reason="Gegenangebot erhalten, weitere Runde innerhalb des Limits moeglich -- bereit fuer naechsten Entwurf.")

    await db.commit()
    return {"matched": True, "classification": classification, "case_status": case.status.value}


@router.post("/internal/test-inbound/{case_id}")
async def test_inbound_inject(
    case_id: str, payload: TestInboundReply,
    membership=Depends(get_current_membership), db: AsyncSession = Depends(get_db),
):
    """NUR fuer Tests/Verifikation: injiziert eine simulierte eingehende
    Antwort, exakt im Format, das der echte IMAP-Poller erzeugen wuerde.
    Durch die normale Membership-Pruefung geschuetzt (kein unauthentifizierter
    oeffentlicher Zugriff), analog zum bestehenden webhooks.router-Muster,
    das ebenfalls keine zusaetzliche Shared-Secret-Pruefung hat."""
    case = await _get_case_or_404(case_id, membership.tenant_id, db)
    r = await db.execute(
        select(EmailMessage)
        .where(EmailMessage.case_id == case.id, EmailMessage.direction == EmailDirection.outbound)
        .order_by(desc(EmailMessage.occurred_at))
    )
    last_outbound = r.scalars().first()
    email_record = {
        "message_id": f"<test-inbound-{uuid.uuid4()}@example-supplier.test>",
        "in_reply_to": last_outbound.message_id if last_outbound else payload.in_reply_to,
        "references_header": last_outbound.message_id if last_outbound else payload.in_reply_to,
        "from_addr": payload.from_addr,
        "to_addr": "info@negotiatex.ai",
        "subject": payload.subject,
        "body_text": payload.body_text,
        "raw_source": "SIMULATED-TEST-INBOUND",
    }
    return await ingest_inbound_message(db, email_record)
