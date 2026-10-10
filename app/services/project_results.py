"""
Ergebnis eines Vorhabens ("Was spare ich?") -- eine Berechnung fuer Kunden-
und Arbeitsbereich.

Regeln (CTO-Vorgabe "Saubere Ergebnisse"):
  - Jede Einsparung hat eine Bezugsgroesse (Ausgangsbasis), einen Zeitpunkt
    und einen Status: Potenzial -> vereinbart -> realisiert.
  - Ausgangsbasis:
      * Einstieg "bestehendes Angebot": das mitgebrachte Ausgangsangebot
        (Erstfassung) des Bestandsanbieters.
      * Einstieg "neuer Bedarf": das Erstangebot des beauftragten Anbieters
        (Einsparung = Verhandlungsergebnis). Unterschiede zu anderen Bietern
        oder zum Budget werden bewusst NICHT als Einsparung gezaehlt.
  - Vereinbart: Ausgangsbasis minus beauftragter Preis (ab Entscheidung).
  - Realisiert: Ausgangsbasis minus tatsaechlich abgerechneter Betrag.
  - Potenzial (nur offene Vorhaben): bereits erreichte, noch nicht
    beauftragte Verbesserung bzw. das bestaetigte Verhandlungsziel.
  - Gebuehr nur, wenn ein Gebuehrensatz vereinbart ist; Nettoeffekt nur bei
    vollstaendigen Daten. Fehlendes = None ("noch nicht ermittelt"), nie 0.
  - Keine Doppelzaehlung: Summen addieren je Vorhaben genau einen Wert je
    Kategorie; "realisiert" ist eine Teilmenge von "vereinbart", Potenzial
    zaehlt nur fuer noch nicht entschiedene Vorhaben.
"""
import uuid
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models_contracts import RFQ, RFQOffer
from models_negotiation import NegotiationStrategy
from models_v2 import Tenant

Q = Decimal("0.01")


def offer_total(o: RFQOffer, quantity_fallback=None) -> Optional[Decimal]:
    if o is None or o.unit_price is None:
        return None
    qty = o.quantity if o.quantity is not None else (quantity_fallback if quantity_fallback is not None else Decimal("1"))
    return (Decimal(o.unit_price) * Decimal(qty) + Decimal(o.freight_cost or 0) + Decimal(o.other_costs or 0)).quantize(Q)


async def project_offers(db: AsyncSession, p) -> list[RFQOffer]:
    if not p.sourcing_request_id:
        return []
    rfq_ids = [r.id for r in (await db.execute(select(RFQ).where(RFQ.sourcing_request_id == p.sourcing_request_id))).scalars().all()]
    if not rfq_ids:
        return []
    return list((await db.execute(select(RFQOffer).where(RFQOffer.rfq_id.in_(rfq_ids)))).scalars().all())


def _first_of(offers: list, candidate_id) -> Optional[RFQOffer]:
    mine = [o for o in offers if o.supplier_candidate_id == candidate_id]
    return min(mine, key=lambda o: o.version) if mine else None


def _money(v) -> Optional[str]:
    return str(v.quantize(Q)) if v is not None else None


async def compute_result(db: AsyncSession, p, tenant: Optional[Tenant] = None) -> dict:
    offers = await project_offers(db, p)
    by_id = {o.id: o for o in offers}
    status = getattr(p.status, "value", p.status)
    currency = p.currency or "EUR"
    baseline = baseline_label = baseline_date = None
    potential = agreed = realized = None
    potential_label = None

    # Ausgangsbasis
    base_offer = None
    if p.entry_path == "existing_offer" and p.incumbent_candidate_id:
        base_offer = _first_of(offers, p.incumbent_candidate_id)
        baseline_label = "Ihr mitgebrachtes Ausgangsangebot"
    awarded = by_id.get(p.awarded_offer_id) if p.awarded_offer_id else None
    if base_offer is None and awarded is not None:
        base_offer = _first_of(offers, awarded.supplier_candidate_id)
        baseline_label = "Erstangebot des beauftragten Anbieters"
    if base_offer is not None:
        baseline = offer_total(base_offer, p.quantity)
        baseline_date = base_offer.received_at

    if awarded is not None:
        awarded_total = offer_total(awarded, p.quantity)
        if baseline is not None and awarded_total is not None:
            agreed = max(baseline - awarded_total, Decimal("0"))
        if p.invoiced_amount is not None and baseline is not None:
            realized = max(baseline - Decimal(p.invoiced_amount), Decimal("0"))
    elif status not in ("cancelled",):
        current = [o for o in offers if getattr(o.status, "value", o.status) == "submitted"]
        improved = []
        for o in current:
            first = _first_of(offers, o.supplier_candidate_id)
            if first is not None and o.version > 1:
                a, b = offer_total(first, p.quantity), offer_total(o, p.quantity)
                if a is not None and b is not None and a > b:
                    improved.append(a - b)
        if improved:
            potential, potential_label = max(improved), "in Verhandlung erreicht, noch nicht beauftragt"
        else:
            for oid, case_id in (p.negotiations_json or {}).items():
                o = next((x for x in offers if str(x.id) == oid), None)
                try:
                    cid = uuid.UUID(str(case_id))
                except ValueError:
                    continue
                strat = (await db.execute(select(NegotiationStrategy).where(NegotiationStrategy.case_id == cid))).scalar_one_or_none()
                if o is not None and strat is not None:
                    gap = Decimal(strat.starting_price_net) - Decimal(strat.round1_price_net)
                    if gap > 0 and (potential is None or gap > potential):
                        potential, potential_label = gap, "Verhandlungsziel (noch nicht erreicht)"

    fee_pct = Decimal(tenant.success_fee_pct) if tenant is not None and tenant.success_fee_pct is not None else None
    fee_basis = realized if realized is not None else agreed
    fee = (fee_basis * fee_pct / Decimal("100")).quantize(Q) if (fee_pct is not None and fee_basis is not None) else None
    net = (fee_basis - fee) if (fee is not None and fee_basis is not None) else None

    if realized is not None:
        state, state_label = "realized", "realisiert"
    elif agreed is not None:
        state, state_label = "agreed", "vereinbart"
    elif potential is not None:
        state, state_label = "potential", "Potenzial"
    else:
        state, state_label = "unknown", "noch nicht ermittelt"

    return {
        "currency": currency, "state": state, "state_label": state_label,
        "baseline": _money(baseline), "baseline_label": baseline_label,
        "baseline_date": baseline_date.isoformat() if baseline_date else None,
        "potential": _money(potential), "potential_label": potential_label,
        "agreed": _money(agreed), "agreed_at": p.awarded_at.isoformat() if (agreed is not None and p.awarded_at) else None,
        "realized": _money(realized), "realized_at": p.invoiced_at.isoformat() if (realized is not None and p.invoiced_at) else None,
        "invoiced_amount": _money(Decimal(p.invoiced_amount)) if p.invoiced_amount is not None else None,
        "fee_pct": str(fee_pct) if fee_pct is not None else None, "fee": _money(fee),
        "fee_provisional": fee is not None and realized is None,
        "net": _money(net),
    }
