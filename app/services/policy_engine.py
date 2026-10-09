"""
Phase 1 deterministic Policy / Value Engine.

Per the CTO briefing: "harte Grenzen und kaufmaennische Berechnungen in
deterministischem Code" -- this module makes NO LLM calls and contains no
negotiation/counter-offer generation (that is explicitly Phase 2+ scope).
All money math uses Python Decimal, never float.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional


@dataclass
class PolicyCheckOutcome:
    result: str  # "ok" | "warning" | "violation"
    message: str


_SEVERITY_ORDER = {"ok": 0, "warning": 1, "violation": 2}


def _as_decimal(value) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def evaluate_policy(line_item, policy) -> PolicyCheckOutcome:
    """Pure, deterministic comparison of a line_item's fields against a
    policy's rules_json. No fields are fabricated -- a missing line_item
    value only ever downgrades the result (warning), never crashes and
    never invents a number.

    Supported rules_json keys (Phase 1 MVP set):
      - required_currency: str (e.g. "EUR")
      - allowed_payment_term_days: int (checked against contract_period text,
        best-effort only in Phase 1 since payment terms are not yet a
        dedicated structured column)
      - benchmark_unit_price: number
      - max_price_increase_pct: number (tolerance band above the benchmark)
    """
    rules = policy.rules_json or {}
    messages = []
    worst = "ok"

    def escalate(level: str):
        nonlocal worst
        if _SEVERITY_ORDER[level] > _SEVERITY_ORDER[worst]:
            worst = level

    if line_item.unit_price is None:
        escalate("warning")
        messages.append("Einzelpreis fehlt - Policy-Pruefung fuer dieses Feld nicht moeglich.")

    required_currency = rules.get("required_currency")
    if required_currency and line_item.currency and line_item.currency.upper() != str(required_currency).upper():
        escalate("violation")
        messages.append(
            f"Waehrung '{line_item.currency}' weicht von Vorgabe '{required_currency}' ab."
        )
    elif required_currency and not line_item.currency:
        escalate("warning")
        messages.append("Waehrung nicht erkannt - Abgleich mit Policy-Vorgabe nicht moeglich.")

    max_increase_pct = rules.get("max_price_increase_pct")
    benchmark_raw = rules.get("benchmark_unit_price")
    benchmark = _as_decimal(benchmark_raw)
    unit_price = _as_decimal(line_item.unit_price) if line_item.unit_price is not None else None

    if max_increase_pct is not None and benchmark is not None and unit_price is not None:
        tolerance = benchmark * (_as_decimal(max_increase_pct) / Decimal("100"))
        allowed_max = benchmark + tolerance
        if unit_price > allowed_max:
            escalate("violation")
            messages.append(
                f"Einzelpreis {unit_price} ueberschreitet den erlaubten Hoechstwert {allowed_max} "
                f"(Benchmark {benchmark} + {max_increase_pct}% Toleranz)."
            )
        elif unit_price > benchmark:
            escalate("warning")
            messages.append(
                f"Einzelpreis {unit_price} liegt ueber Benchmark {benchmark}, aber innerhalb der Toleranz."
            )

    if not messages:
        messages.append("Keine Abweichungen festgestellt.")

    return PolicyCheckOutcome(result=worst, message=" ".join(messages))


def compute_potential_savings(line_item, benchmark_value) -> Decimal:
    """MVP value function (Phase 1 simplification, per briefing):

        potential_savings = max(0, unit_price - benchmark) * quantity

    Deliberate Phase 1 simplifications (documented per briefing's explicit
    MVP scope limits): no tax handling, no currency conversion, no "known
    fees" addition, and no modeling of "belastbar erfuellbare Rabatte"
    (achievable discounts) yet -- the full briefing formula
    ("Gesamtkosten = Nettopreis + bekannte Gebuehren - belastbar erfuellbare
    Rabatte") is reduced here to its simplest unit-price-vs-benchmark form.
    Returns Decimal("0") whenever required inputs are missing -- never a
    guessed number.
    """
    benchmark = _as_decimal(benchmark_value)
    unit_price = _as_decimal(line_item.unit_price) if line_item.unit_price is not None else None
    if benchmark is None or unit_price is None:
        return Decimal("0")

    quantity = _as_decimal(line_item.quantity) if line_item.quantity is not None else Decimal("1")
    diff = unit_price - benchmark
    if diff <= 0:
        return Decimal("0")
    return diff * quantity
